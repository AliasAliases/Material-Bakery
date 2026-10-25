# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   运行期间的偏好设置护栏：暂时关掉"全局撤销"和"自动保存"
#
#   为什么（用户实测，2026-09）:
#       他 8/8 烘完之后 Blender 有 729 秒完全没响应，**内存是平的**，最后只能强杀。
#       内存平、时间以分钟计 —— 这是**磁盘写入**的特征，不是计算的。
#       那个窗口里唯一会一次写好几个 GB 的磁盘操作就是 Blender 的自动保存：
#       他的文件打包后 6 GB，而自动保存（默认每 2 分钟一次）会把整个文件
#       重写一份到临时目录。烘焙把文件标记成"有改动"，主线程一直被烘焙占着，
#       所以那个定时器只能在**烘焙刚结束**的时候补上 —— 和现象完全对得上。
#       撤销是同一个毛病的另一面：`undo_memory_limit = 0` 表示每一步都永远留着，
#       而这一步之后新建的是几百 MB 的烘焙图像。
#
#   所以：烘焙在跑的时候把这两样停掉，**每一条退出路径**都恢复
#   （跑完 / 取消 / 失败 / 注销插件 / 换文件）。恢复是这一个模块存在的意义 ——
#   偷偷改掉用户的偏好设置，比它避免的那次卡顿更糟。
#
#   ⚠ 只动这两个开关，不碰 undo_steps / undo_memory_limit / auto_save_time：
#     没必要改的一律不改，恢复起来也就没有"改错了"的可能。
# ------------------------------------------------------------------------------------

import bpy


def _preferences(context=None):
    context = context if context is not None else bpy.context
    preferences = getattr(context, "preferences", None)
    if preferences is None:
        preferences = getattr(bpy.context, "preferences", None)
    return preferences


class PreferenceGuard:
    """把用户的偏好设置借走一会儿，然后**一定**还回去。"""

    # (section, attribute) —— 要暂停的两个开关
    UNDO = ("edit", "use_global_undo")
    AUTOSAVE = ("filepaths", "use_auto_save_temporary_files")

    def __init__(self):
        self._saved = None
        self._preferences = None    # 借的是**哪一个**偏好对象，就还给哪一个
        self.messages = []          # [(级别, 文案)]，调用方取走写进会话日志

    # -- 状态 -------------------------------------------------------------------

    @property
    def engaged(self):
        return self._saved is not None

    def drain_messages(self):
        messages = self.messages
        self.messages = []
        return messages

    def _say(self, level, message):
        self.messages.append((level, message))

    # -- 借 ---------------------------------------------------------------------

    def engage(self, context=None):
        """暂停全局撤销与自动保存。已经借过就什么都不做。

        返回是否真的借到了（没有偏好设置时返回 False，调用方据此决定要不要
        在日志里说"没能暂停"）。
        """
        if self._saved is not None:
            return True
        preferences = _preferences(context)
        if preferences is None:
            self._say("WARN", "No preferences available — global undo and auto-save "
                              "were left as they are")
            return False

        saved = {}
        for section_name, attribute in (self.UNDO, self.AUTOSAVE):
            section = getattr(preferences, section_name, None)
            if section is None or not hasattr(section, attribute):
                continue
            saved[(section_name, attribute)] = bool(getattr(section, attribute))
        if not saved:
            self._say("WARN", "This Blender has neither 'use_global_undo' nor "
                              "'use_auto_save_temporary_files' — nothing to pause")
            return False

        self._saved = saved
        self._preferences = preferences
        for (section_name, attribute), _value in saved.items():
            try:
                setattr(getattr(preferences, section_name), attribute, False)
            except (AttributeError, TypeError) as exc:
                self._saved.pop((section_name, attribute), None)
                self._say("WARN", "Could not pause {}.{}: {}".format(
                    section_name, attribute, exc))
        if not self._saved:
            self._preferences = None
            return False

        paused = ", ".join("{}.{}".format(section, attribute)
                           for section, attribute in sorted(self._saved))
        self._say("INFO", "Paused for this run: {} — the long freeze right after a bake "
                          "looks like a multi-GB auto-save write plus an unbounded undo "
                          "step, so both are off while baking. Both are put back when "
                          "the run ends".format(paused))
        return True

    # -- 还 ---------------------------------------------------------------------

    def release(self):
        """把借走的东西还回去。幂等，而且**绝不抛异常**。

        ⚠ 为什么强调不抛：这个方法挂在退出的每一条路径上（跑完、取消、失败、
          注销插件、换文件）。任何一条路径上抛异常，用户的偏好就永久停在那，
          而这正是这个方法要避免的事。
        """
        saved = self._saved
        preferences = self._preferences
        self._saved = None
        self._preferences = None
        if not saved:
            return False
        if preferences is None:
            preferences = _preferences()
        if preferences is None:
            self._say("WARN", "Preferences are gone — the undo / auto-save switches "
                              "could not be put back (restart Blender to be safe)")
            return False
        restored = []
        for (section_name, attribute), value in sorted(saved.items()):
            try:
                setattr(getattr(preferences, section_name), attribute, value)
                restored.append("{}.{}={}".format(section_name, attribute, value))
            except (AttributeError, TypeError, RuntimeError) as exc:
                self._say("ERROR", "Could not restore {}.{}: {}".format(
                    section_name, attribute, exc))
        if restored:
            self._say("INFO", "Put back: {}".format(", ".join(restored)))
        return bool(restored)

    # -- 展示 -------------------------------------------------------------------

    def describe(self):
        if not self._saved:
            return "undo and auto-save are untouched"
        return "paused: {}".format(", ".join(
            "{}.{}".format(section, attribute)
            for section, attribute in sorted(self._saved)))

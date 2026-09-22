# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   烘焙任务：一个可被反复 step() 的状态机
#
#   为什么不做成「一路跑到底的函数」:
#       GUI 里 bpy.ops.object.bake 是异步 job，函数必须能中途把控制权交回 Blender，
#       否则界面卡死、进度条也没法画。step() 每次只推进一小步，UI 用 timer 驱动。
#
#   三条铁律（都是旧引擎踩过的坑）:
#       1. 任何对场景的写入都发生在派发烘焙**之前**；烘的过程中一个字节都不写。
#          旧引擎在派发后写字符串 IDProperty，撞上 bake job 线程的 depsgraph COW 拷贝，
#          直接在 IDP_CopyProperty_ex 里 use-after-free 崩掉。
#       2. 没有模块级全局变量。两个烘焙列表同时跑也不会互相踩。
#       3. 所有临时改动走 SceneTransaction，异常/取消都还原。
# ------------------------------------------------------------------------------------

import time

import bpy

from .. import compat
from ..core import phases
from ..core import naming
from ..core import plan as plan_mod
from ..core import transaction as transaction_mod
from . import backend as backend_mod
from . import report as report_mod
from . import uv_pack
from . import uv_prep


STATE_IDLE = "idle"
STATE_PREPARE = "prepare"
STATE_BAKING = "baking"
STATE_WAITING = "waiting"          # 已派发，等 bake job 结束
STATE_PAUSED = "paused"
STATE_FINISH = "finish"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

TERMINAL = (STATE_DONE, STATE_CANCELLED, STATE_FAILED)

# 等一个 bake job 最多等多久（秒）。超过就判定它不会回来了。
#
# ⚠ 这是从用户反馈里学来的：他报"烘焙完成了会卡死，只能强杀 Blender"。
#   原因很清楚 —— 卡在 STATE_WAITING 时**没有任何超时**，而 backend.abort()
#   又杀不掉 Blender 自己的 bake job，于是模态 operator 永远不结束、
#   界面被它吃掉输入，用户只能强杀进程。
#   120s 的依据：用户那轮 128 张 2048² 的实测里，**最慢的一张 37.9 秒**、
#   平均 10.1 秒（见 MATERIAL_BAKERY_DESIGN.md 第 21 节）。2 分钟已经是
#   正常单张的三倍以上，真卡住了也不用干等 10 分钟。
WAIT_TIMEOUT = 120.0
WAIT_HEARTBEAT = 5.0               # 每隔这么久报一次"还在等"（写日志，看得见）


class BakeJob:
    """一次烘焙运行。创建后反复调用 step() 直到 is_finished。"""

    def __init__(self, context, plan, settings, backend=None, transaction=None,
                 label="", prepare_uv=None, on_task_done=None, on_before_dispatch=None):
        self.context = context
        self.plan = plan
        self.settings = settings
        self.report = report_mod.RunReport(label=label or "bake")
        # ⚠ 事务必须跟后端是**同一个**。
        #   后端拿着 tx 去改场景（选物体、挂节点、切引擎），job 拿着 tx 去快照/还原。
        #   两边各建一个的话，后端那个从没 capture 过，一旦走到投影烘焙
        #   （要临时取消隐藏源物体）就会崩。
        #   所以：显式传的优先；否则沿用后端已有的那个；再否则才自己建。
        if transaction is not None:
            self.tx = transaction
        elif getattr(backend, "tx", None) is not None:
            self.tx = backend.tx
        else:
            self.tx = transaction_mod.SceneTransaction(context)
        self.backend = backend if backend is not None else backend_mod.NullBackend()
        self.prepare_uv = settings.prepare_uv if prepare_uv is None else prepare_uv
        self.on_task_done = on_task_done
        # 派发**之前**的回调：UI 用它把状态推上去、强制重绘一次。
        # 为什么需要：GUI 里 bpy.ops.object.bake 是同步阻塞的（实测调用返回时
        # 已经烘完），主线程在那一整段时间里一帧都画不出来。没有这个回调，
        # 用户看到的就是"界面突然不动了"，而且不知道卡住的是哪一张。
        self.on_before_dispatch = on_before_dispatch

        self.state = STATE_IDLE
        self.index = 0
        self.prepare_index = 0
        self._prepared_groups = []
        self.paused = False
        self._started_tx = False
        self._dispatched = None         # 正在烘的任务
        self._cancel_requested = False
        self._details = []
        self.waiting_since = 0.0        # 进入 STATE_WAITING 的时刻
        self._last_heartbeat = 0.0
        self.events = []                # 给人看的进度事件，UI 每步取走

    # -- 进度事件 ---------------------------------------------------------------

    def _emit(self, level, message):
        """记一条进度事件。

        存在的理由：用户报"卡死"时，我们手里**什么都没有** ——
        日志里只有开场一行和结尾一行，中间是黑箱，于是只能靠猜。
        现在每个任务开始/结束/等待都有记录，下次出问题现场自己就留下了。
        """
        self.events.append((level, message))
        compat.log("[{}] {}".format(self.report.label, message))

    def drain_events(self):
        """取走累积的事件（UI 在每一步之后调用）"""
        events = self.events
        self.events = []
        return events

    # -- 查询 -------------------------------------------------------------------

    @property
    def is_finished(self):
        return self.state in TERMINAL

    @property
    def is_running(self):
        return self.state in (STATE_PREPARE, STATE_BAKING, STATE_WAITING, STATE_PAUSED,
                              STATE_FINISH)

    @property
    def total(self):
        return len(self.plan.tasks)

    @property
    def progress(self):
        """(已完成的工作量, 总工作量, 比例)

        ⚠ 把"准备"也算进总工作量：准备阶段（展 UV / 打包 UV / 建图像）
          在真实场景里可能占掉整个烘焙相当一部分时间，而且它一步一个任务。
          只按"已烘好的贴图数"算的话，进度条会在准备阶段**一动不动**，
          看起来就像卡死了 —— 用户就是这么报的。
        """
        total = self.total
        if not total:
            return 0, 0, 1.0
        prepared = min(self.prepare_index, total)
        finished = sum(1 for t in self.plan.tasks
                       if t.status in ("done", "failed", "skipped"))
        work_total = total * 2
        work_done = prepared + finished
        return work_done, work_total, work_done / float(work_total)

    @property
    def phase(self):
        """给 UI 用的粗粒度阶段。准备已经并进烘焙步骤，所以只有两个阶段。"""
        if self.state in (STATE_BAKING, STATE_WAITING, STATE_PAUSED, STATE_PREPARE):
            return "baking"
        return "done"


    def current_task(self):
        if self._dispatched is not None:
            return self._dispatched
        if 0 <= self.index < self.total:
            return self.plan.tasks[self.index]
        return None

    def eta(self):
        """按已完成任务的实测速度估剩余秒数；样本不足时返回 None"""
        finished = [r for r in self.report.results if r.status == report_mod.STATUS_DONE]
        if not finished:
            return None
        spent = sum(r.seconds for r in finished)
        if spent <= 0:
            return None
        average = spent / len(finished)
        remaining = self.total - len(finished)
        return max(0.0, average * remaining)

    def last_seconds(self):
        """上一张烘了多久。没有样本时返回 0.0。

        为什么值得单独算：派发是阻塞的，用户唯一能看到的"进度感"就是
        "上一张花了多久 + 大概还剩多久"。只写 "baking 8/128" 的话，
        在每张 10 秒的场景里那就是 20 分钟的纯等待，没有任何预期。
        """
        done = [r for r in self.report.results if r.status == report_mod.STATUS_DONE]
        return float(done[-1].seconds) if done else 0.0

    def eta_text(self):
        """给人看的"上一张多久 / 还剩多久"；没样本就是空串"""
        last = self.last_seconds()
        if not last:
            return ""
        remaining = self.eta()
        if remaining is None:
            return "last one took {:.0f}s".format(last)
        return "last one took {:.0f}s, ~{:.0f}s to go".format(last, remaining)

    def _dispatch_status(self, task):
        text = "baking {}/{}: {} — {} @{}px".format(
            self.index, self.total, task.group_name, task.bake_type.label,
            task.size_label)
        eta = self.eta_text()
        return "{} ({})".format(text, eta) if eta else text

    def _before_dispatch(self, task):
        """派发前通知 UI（回调里的异常绝不允许把烘焙搞挂）"""
        hook = getattr(self, "on_before_dispatch", None)
        if hook is None:
            return
        try:
            hook(task)
        except Exception as exc:
            compat.log("on_before_dispatch 回调出错: {}".format(exc))

    # -- 驱动 -------------------------------------------------------------------

    def start(self):
        if self.state != STATE_IDLE:
            return self.state
        self.report.start()
        self.report.planned = len(self.plan.tasks)

        if self.plan.is_empty:
            self.report.warn("Nothing to bake.")
            self.state = STATE_FINISH
            return self.state

        self.tx.capture()
        self._started_tx = True

        # ⚠ 重活一个都不在这里做。
        #   展 UV、打包 UV、建图像，原来全在这一步同步跑完 ——
        #   实测 32 集合 × 3 张 2048² 的场景，光建 96 张图像就要 1.7s
        #   并一次性申请约 4.5 GB 像素内存。界面上表现为：点下"开始烘焙"之后
        #   整个界面卡死，进度条不动，暂停和取消按钮**根本点不到**。
        #   现在这些全部挪进 PREPARE 阶段，一步一个任务，随时可暂停/取消。
        try:
            self.backend.begin(self.plan, self.settings)
        except Exception as exc:
            self._fail_fatal("backend could not start: {}".format(exc))
            return self.state

        # 内存估算：按实测斜率算，超过 2 GB 就先把话说在前头。
        # 数字来自 tools/measure_bake_memory.py 的真机测量，不是按尺寸猜的。
        warning = plan_mod.memory_warning(self.plan.tasks)
        if warning:
            self.report.warn(warning)

        # 不搞"先全准备、再全烘"：准备跟着每个任务走（见 _step_bake）
        self.state = STATE_BAKING
        return self.state

    def step(self):
        """推进一步。返回值是推进后的状态。"""
        if self.state == STATE_IDLE:
            self.start()
            if self.state in TERMINAL:
                return self.state

        if self._cancel_requested and self.state not in TERMINAL:
            return self._cancel()

        # 暂停：立刻停下不再派发新任务。
        # 唯一的例外是 STATE_WAITING —— 那一张已经在 Cycles 里烘了，
        # 半路打断只会留下一张废图，所以让它烘完再停。
        if self.paused and self.state != STATE_WAITING:
            self.state = STATE_PAUSED
            return self.state
        if not self.paused and self.state == STATE_PAUSED:
            self.state = STATE_BAKING

        try:
            if self.state == STATE_PREPARE:
                return self._step_prepare()
            if self.state == STATE_BAKING:
                return self._step_bake()
            if self.state == STATE_WAITING:
                return self._step_wait()
            if self.state == STATE_FINISH:
                return self._step_finish()
        except Exception as exc:                      # 兜底：绝不让异常冒到 UI
            self._fail_fatal("{}: {}".format(type(exc).__name__, exc))
        return self.state

    def run_to_completion(self, max_steps=100000):
        """一口气跑完 —— 背景模式与测试用"""
        guard = 0
        while not self.is_finished and guard < max_steps:
            self.step()
            guard += 1
        if not self.is_finished:
            self._fail_fatal("exceeded {} steps".format(max_steps))
        return self.report

    def pause(self):
        self.paused = True
        return self.state

    def resume(self):
        self.paused = False
        if self.state == STATE_PAUSED:
            self.state = STATE_BAKING
        return self.state

    def cancel(self):
        self._cancel_requested = True
        return self.state

    # -- 各阶段 -----------------------------------------------------------------

    def _prepare_group(self, group_name):
        """一个分组的准备工作：展 UV + 打包 UV。**每组只做一次。**

        按组做而不是一次做完所有组：真实场景里 32 个集合一起展 UV / 打包
        可能要几十秒，那期间界面完全没反应。按组做，每步之间的间隙
        足够 UI 重绘、也足够用户按暂停或取消。
        """
        if group_name in self._prepared_groups:
            return
        self._prepared_groups.append(group_name)

        group = None
        for candidate in self.plan.groups:
            if candidate.name == group_name:
                group = candidate
                break
        if group is None:
            return

        if self.prepare_uv:
            # 只给**没有 UV** 的物体展 UV；展到哪一层由用户在第 3 页选
            # （`UVMap` 或新建一层 `MBAKERY_UV`）。已有 UV 的物体永远不动。
            try:
                unwrapped = uv_prep.ensure_uvs(
                    self.context, group.objects,
                    uv_name=getattr(self.settings, "unwrap_target", "UVMap") or "UVMap")
            except Exception as exc:
                self.report.warn("Prepare UV failed for {}: {}".format(group_name, exc))
                unwrapped = []
            if unwrapped:
                self.report.warn("Unwrapped {} object(s) in {} into '{}': {}".format(
                    len(unwrapped), group_name,
                    getattr(self.settings, "unwrap_target", "UVMap"),
                    ", ".join(unwrapped[:5])))
                self.plan.warnings.append(
                    "Prepared UVs for {} object(s) in {}".format(
                        len(unwrapped), group_name))

        # ⚠ 这里以前还有一步"把多个物体的 UV 打包进互不重叠的格子"
        #   （uv_pack.pack_shared_uv → 新建 MBAKERY_UV 层）。
        #   用户 2026-09 明确要求**删掉这条路**：
        #     他要的是"一个集合一张贴图，用他现在的 UV，一个字节都不动"，
        #     他的 UV 是故意重叠的（镜像件共用一块纹理），打包既治不了重叠、
        #     又平白多出一层 MBAKERY_UV，还浪费大量空间。
        #   代价（已在设计文档里写明）：如果重叠的两块区域需要的内容不同，
        #   后烘的会覆盖先烘的 —— 这是这种做法的固有代价，由用户自己把握。
        #   现在 `MBAKERY_UV` 只有一个来源：用户主动选"展到新层"。

    def _step_prepare(self):
        """保留这个方法只为兼容旧的调用点；准备已经并进 _step_bake 了。

        「先全准备、再全烘」这个两段式结构就是卡死的根源，不要再用。
        """
        self.state = STATE_BAKING
        return self._step_bake()

    def _step_bake(self):
        """一步一个任务：**先准备这一个，再派发它**。

        ⚠ 这是从原版 Auto Bake 学来的关键一条。
          原版的模态循环是"建第 1 张图 → 烘第 1 张 → 建第 2 张图 → 烘第 2 张 …"，
          所以它第一张图建完就开始烘、主线程每个 tick 只干一点活、界面全程可用。

          我原来是把 96 张图先全建出来再开始烘 —— 同一个机制，
          但一次 tick 的工作量差了 96 倍（实测 1.7s、4.5 GB），界面直接卡死。
          改成交错之后，第一张图建完就开始烘，峰值内存也不再是一瞬间的尖峰。
        """
        if self.index >= self.total:
            self.state = STATE_FINISH
            return self._step_finish()

        task = self.plan.tasks[self.index]
        self.index += 1
        if task.status == "failed":
            return self.state                 # 准备阶段就失败了，账已经记过

        if not self._prepare_task(task):
            return self.state

        started = time.time()
        task.status = "baking"
        self._emit("INFO", self._dispatch_status(task))
        # ⚠ 顺序不能动：状态先写好 → 让 UI 重绘 → 才派发（派发是阻塞的）。
        self._before_dispatch(task)
        self._shift_uvs_for_task(task)
        # ⚠ 这一行到下一条日志之间就是**主线程被后端占满**的整段时间
        #   （Cycles 是同步阻塞的），也就是用户眼里"界面不动"的那一段。
        #   计时放在这里而不是后端里，是为了让任何后端都留下数字。
        phases.begin("dispatch {}".format(task.image_name))
        try:
            done = self.backend.dispatch(task)
        except Exception as exc:
            phases.end("dispatch {}".format(task.image_name), "raised")
            self._restore_uvs_for_task()
            task.status = "failed"
            task.error = "{}: {}".format(type(exc).__name__, exc)
            self._emit("FAIL", "{} — {}: {}".format(
                task.group_name, task.bake_type.label, task.error.strip()))
            self._record(task, report_mod.STATUS_FAILED, task.error,
                         seconds=time.time() - started)
            return self.state
        phases.end("dispatch {}".format(task.image_name))

        self._dispatched = task
        self._dispatched_started = started
        if done:
            self._complete_dispatched()
            return self.state
        self.state = STATE_WAITING
        self.waiting_since = time.time()
        self._last_heartbeat = 0.0
        return self.state

    def _prepare_task(self, task):
        """准备这一个任务：该组的 UV 准备/打包 + 建这一张图

        返回 True 表示可以烘了。组级准备每组只做一次（`_prepared_groups` 记着）。
        """
        if task.image is not None:
            return True
        self.prepare_index += 1
        try:
            self._prepare_group(task.group_name)
            self.backend.prepare(task)
        except Exception as exc:
            task.status = "failed"
            task.error = "prepare: {}".format(exc)
            self._emit("FAIL", "{} — {}: {}".format(
                task.group_name, task.bake_type.label, task.error))
            self._record(task, report_mod.STATUS_FAILED, task.error)
            return False
        return True

    def waited_seconds(self):
        """当前这个任务已经等了多久（不在等待状态则为 0）"""
        if self.state != STATE_WAITING or not self.waiting_since:
            return 0.0
        return time.time() - self.waiting_since

    def _step_wait(self):
        """等 Blender 的 bake job 结束 —— **带超时和心跳**。

        ⚠ 这是用户报的"卡死"的正解。原来这里只有一句
          `if self.backend.poll(): return self.state`：
          万一那个 job 再也不结束（崩溃、被别的模态顶掉、驱动层面的意外），
          整个向导就永远停在这一步，界面被模态吃掉输入，只能强杀 Blender，
          而且日志里一个字都没有。
          现在：等的时候每 5 秒写一条心跳（用户看得见在等什么、等了多久），
          超过 WAIT_TIMEOUT 就判定这个 job 回不来了 —— 把这张图记为失败、
          写清楚原因，然后**继续跑后面的**，把控制权还给界面。
        """
        if not self.backend.poll():
            self._complete_dispatched()
            if self.state == STATE_WAITING:
                self.state = STATE_BAKING
            self.waiting_since = 0.0
            return self.state

        waited = self.waited_seconds()
        if waited - self._last_heartbeat >= WAIT_HEARTBEAT:
            self._last_heartbeat = waited
            task = self.current_task()
            label = "{} — {}".format(task.group_name, task.bake_type.label) \
                if task is not None else "the bake job"
            self._emit("WAIT", "still waiting for Blender's bake job "
                               "({}): {:.0f}s".format(label, waited))
        if waited >= self.wait_timeout():
            task = self._dispatched
            self._dispatched = None
            self.waiting_since = 0.0
            self._restore_uvs_for_task()
            if task is not None:
                task.status = "failed"
                task.error = ("the Cycles bake job did not return within {:.0f}s "
                              "and was abandoned".format(waited))
                self._emit("FAIL", "gave up on {} — {} after {:.0f}s".format(
                    task.group_name, task.bake_type.label, waited))
                self._record(task, report_mod.STATUS_FAILED, task.error, seconds=waited)
            # ⚠ 这里**收尾而不是取消**。
            #   取消会 rollback 事务，把已经打包好的共享 UV 层也一起撤掉 ——
            #   而那些图已经烘好、材质还要靠那层 UV 采样，撤掉就等于
            #   "交付出来的材质全是错位的"。收尾（commit）才能保住已完成的部分。
            for pending in self.plan.tasks[self.index:]:
                if pending.status in ("done", "failed", "skipped"):
                    continue
                pending.status = "skipped"
                self.report.skip(pending.group_name,
                                 "not baked — a previous map stopped responding")
            self.report.warn(
                "A Cycles bake job stopped responding and the remaining maps were "
                "skipped after {:.0f}s. Everything baked so far is kept. Try fewer maps, "
                "a smaller resolution, or bake one collection at a time.".format(waited))
            self.state = STATE_FINISH
            return self._step_finish()
        return self.state

    def wait_timeout(self):
        """超时阈值 —— 实例属性优先（测试要能缩短它）

        ⚠ 不能写 `getattr(..., WAIT_TIMEOUT) or WAIT_TIMEOUT`：
           那样传 0.0 会被当成"没设置"，测试里就把默认的 10 分钟拿了回来
           （第一次写就是这么错的，超时测试直接不动）。
        """
        value = getattr(self, "wait_timeout_seconds", None)
        if value is None:
            return float(WAIT_TIMEOUT)
        return float(value)

    def _complete_dispatched(self):
        task = self._dispatched
        self._dispatched = None
        if task is None:
            return
        started = getattr(self, "_dispatched_started", time.time())
        self._restore_uvs_for_task()
        self._apply_antialias(task)
        self._secure_image(task)
        try:
            self.backend.collect(task)
        except Exception as exc:
            task.status = "failed"
            task.error = "save: {}".format(exc)
            self._record(task, report_mod.STATUS_FAILED, task.error,
                         seconds=time.time() - started)
            return
        task.status = "done"
        if task.exported_path:
            self.plan.name_preview.append((task.group_name, task.exported_path))
        seconds = time.time() - started
        self._record(task, report_mod.STATUS_DONE, seconds=seconds)
        phases.mark("map done: {} in {:.2f}s".format(task.image_name, seconds))
        if self.on_task_done:
            try:
                self.on_task_done(task)
            except Exception as exc:
                compat.log("on_task_done 回调出错: {}".format(exc))

    def _step_finish(self):
        # ⚠ 这一段被计时逐段记录（见 core/phases.py）。用户那次"8/8 之后 729 秒
        #   没响应"就发生在这里 + 交付阶段，而日志对此一片空白，只能靠猜。
        phases.begin("finish: commit transaction")
        if self._started_tx and self.tx is not None:
            self.tx.commit()                # 保留新建的图像节点与材质
        phases.end("finish: commit transaction")
        phases.begin("finish: report")
        self._warn_dropped_params()
        for group, reason in self.plan.skipped:
            self.report.skip(group, reason)
        for message in self.plan.warnings:
            self.report.warn(message)
        self.report.conflicts = list(self.plan.conflicts)
        self.report.stop()
        phases.end("finish: report")
        self.state = STATE_DONE
        return self.state

    def _cancel(self):
        try:
            self.backend.abort()
        except Exception as exc:
            compat.log("abort 出错: {}".format(exc))
        if self._dispatched is not None:
            self._record(self._dispatched, report_mod.STATUS_CANCELLED)
            self._dispatched = None
        self._restore_uvs_for_task()
        if self._started_tx and self.tx is not None:
            self.tx.rollback()
            self._started_tx = False
        self.report.cancelled = True
        self.report.stop()
        self.state = STATE_CANCELLED
        return self.state

    def _fail_fatal(self, message):
        self.report.fatal = message
        if self._started_tx and self.tx is not None:
            self.tx.rollback()
            self._started_tx = False
        self.report.stop()
        self.state = STATE_FAILED
        compat.log("烘焙失败: {}".format(message))

    # -- 记账 -------------------------------------------------------------------

    def _shift_uvs_for_task(self, task):
        """UDIM：把目标瓦片平移到 0..1，烘完再精确还原

        Blender 的烘焙只写 0..1 范围内的像素，所以把 UV 平移之后，
        只有目标瓦片的内容会落进这张图 —— 这就等于"只烘这一张瓦片"。
        """
        self._uv_snapshot = None
        shift = getattr(task, "uv_shift", ())
        if not shift:
            return
        self._uv_snapshot = uv_prep.snapshot_uvs(task.targets)
        uv_prep.shift_uvs(task.targets, shift[0], shift[1])

    def _restore_uvs_for_task(self):
        snapshot = getattr(self, "_uv_snapshot", None)
        if snapshot is None:
            return
        try:
            uv_prep.restore_uvs(snapshot)
        except Exception as exc:
            compat.log("还原 UDIM UV 失败: {}".format(exc))
        self._uv_snapshot = None

    def _apply_antialias(self, task):
        """把超采样烘出来的大图缩回交付尺寸。

        ⚠ 必须在 pack 之前做：缩图会改像素缓冲区，pack 之后就固定住了。
        """
        image = task.image
        if image is None:
            return
        mode = getattr(self.settings, "antialias", 'OFF')
        if mode == 'OFF':
            return

        if mode == 'DOWNSCALE':
            target_x, target_y = task.size_x, task.size_y
        elif mode == 'UPSCALE':
            scale = max(1, int(getattr(self.settings, "aa_scale", 2) or 1))
            target_x, target_y = task.size_x * scale, task.size_y * scale
        else:
            return

        if image.size[0] == target_x and image.size[1] == target_y:
            return
        try:
            image.scale(target_x, target_y)
        except RuntimeError as exc:
            self.report.warn("Antialiasing scale failed for {}: {}".format(
                task.image_name, exc))
            return
        if mode == 'UPSCALE':
            self.report.warn("{} was rendered at {} and upscaled to {}".format(
                task.image_name, naming.size_label(task.size_x, task.size_y),
                naming.size_label(target_x, target_y)))

    def _secure_image(self, task):
        """在清理临时烘焙节点之前，先把图像数据固定下来。

        顺序很关键：commit() 会移除材质里的临时烘焙节点，那张图就没人引用了；
        而 2048² 的生成图像一旦写过盘，Blender 就会释放像素缓冲区 ——
        之后再导出会报 "does not have any image data"，reload() 也救不回来。
        pack 之后数据存在 .blend 里，第 4 页想导出几次都行。
        """
        image = task.image
        if image is None:
            return
        if self.settings.fake_user:
            image.use_fake_user = True
        if self.settings.pack_into_blend and (image.packed_file is None or image.is_dirty):
            # 先把格式钉死：float 图像 pack() 默认会存成 OPEN_EXR，
            # 之后导出就会写出 .exr 而不是用户选的 PNG。
            image.file_format = self.settings.file_format
            try:
                image.pack()
            except RuntimeError as exc:
                self.report.warn("Could not pack {}: {}".format(image.name, exc))

    def _warn_dropped_params(self):
        """后端有参数被这个 Blender 版本丢掉的话，明确说出来

        静默丢弃是最糟的一种失败：用户改了设置、点了烘焙、结果不对，
        但没有任何地方告诉他"你改的那个参数根本没生效"。
        """
        dropped = getattr(self.backend, "dropped_params", None)
        if not dropped:
            return
        self.report.warn(
            "This Blender build does not accept these bake parameters, they were "
            "ignored: {}".format(", ".join(dropped)))
        compat.log("被丢弃的 bake 参数: {}".format(dropped))

    def _record(self, task, status, error="", seconds=0.0):
        result = report_mod.TaskResult(
            group=task.group_name,
            type_key=task.bake_type.key,
            type_label=task.bake_type.label,
            size_x=task.size_x,
            size_y=task.size_y,
            image_name=task.image_name,
            filepath=task.exported_path,
            status=status,
            error=error,
            seconds=seconds,
            objects=[o.name for o in task.targets],
        )
        result.surgery = list(task.surgery_detail)
        self.report.add(result)
        return result

    # -- 展示 -------------------------------------------------------------------

    def status_line(self):
        task = self.current_task()
        if task is not None and task.status == "pending" and self.state == STATE_BAKING:
            return "Preparing {} — {}".format(task.group_name, task.bake_type.label)
        if self.state == STATE_PREPARE:
            return "Preparing..."
        eta = self.eta_text()
        if self.state == STATE_WAITING and task is not None:
            # 等真烘完：这里是在**阻塞中**被读到的（面板在下一次重绘时取值）
            return "Baking {} — {} ({:.0f}s in)".format(
                task.group_name, task.bake_type.label, self.waited_seconds())
        if self.state == STATE_BAKING and task is not None:
            text = "Baking {} — {}".format(task.group_name, task.bake_type.label)
            return "{} ({})".format(text, eta) if eta else text
        if self.state == STATE_PAUSED:
            return "Paused — {} of {} prepared".format(
                min(self.prepare_index, self.total), self.total)
        if self.state == STATE_DONE:
            return self.report.summary()
        return self.state

    def __repr__(self):
        return "<BakeJob {} {}/{}>".format(self.state, self.progress[0], self.total)

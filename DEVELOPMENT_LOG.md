# DEVELOPMENT_LOG — Material Bakery 开发日志

> **这是给我自己（AI）用的开发日志，不是给用户看的文档。**
> - 用户要看的说明在 `README.md` / `INSTALL.md`；**除非用户明确要求，不要动那两份**。
> - 其他会话读这份时：**先看最后一节「当前状态」，再看最近的条目**；已经标了 ✅ 的条目不用重读细节，那是已完成的历史，读了只是浪费上下文。
> - 设计权威是 `MATERIAL_BAKERY_DESIGN.md`（2446 行，完整设计手册）。这份日志只记**开发过程**，不重复设计。
> - 工作规矩见本文件末尾「协作规矩」，那是用户的原始约定，任何会话都必须遵守。

---

## 当前状态（最新在最上）

**阶段**：主要开发流程已完成；代码已上过 GitHub；此后进入**日常使用中的 bug 修复**阶段。

**分支 / 版本**：`(1, 1, 0)`；两个仓库 `origin` = Material-Bakery-QuadRemesher（私有，源码真相）、`public` = Material-Bakery（公开变体，由 `tools/make_public_variant/strip_qr.py` 生成）。

**双变体维护**（用户 2026-09-25 明确）：**两边都要维护、两边都要提交改动**。初始版本是私有库，因为有一页功能会驱动 QuadRemesher（闭源插件），只能放私有仓库。

**打包约定**（用户 2026-09-25 明确）：**以后做出改动要记得打包**，产物名 `material_bakery_install.zip`。流程：
```
powershell -File tools\sync_package.ps1     # 工作树 -> MaterialBakery\material_bakery
powershell -File tools\build_zips.ps1       # -> MaterialBakery\*_install.zip
```

**本机已安装插件的位置**：`%APPDATA%\Blender Foundation\Blender\4.5\scripts\addons\material_bakery`
—— ⚠ Blender **只从这里加载**，工作树改了不等于用户跑得到。2026-09-25 已把工作树镜像过去；原公开版备份在 `material_bakery.backup-20260925`（用户说"改了就改了"，不必撤）。

**未决 / 待办**：
- 材质级拆分（用户 2026-09-23 提出，尚未开工）：4 材质物体要把 2 个烘到贴图 A、2 个烘到贴图 B，且不拆物体。等他答完三个问题（目的是纹素密度还是材质分开 / 两组名字 / 是否自动接线）并说「开始」。

---

## 2026-09-25 · 修：烘焙完成时 GUI 卡死，只能强杀 Blender ✅

### 用户需求
> "我现在每次烘焙完成的时候都会卡住" + 一份运行日志 + 一份 Blender 控制台日志。卡死之后只能强杀重启。后来 07:09、07:37 又各复现一次。

### 修改过程
1. **缩范围**：运行日志最后一行是 `restore: engine + bake settings: start`，**没有配对的 `done in Xs`** → 卡死在 `core/transaction.py::_restore_scene` 的 20 行还原赋值里。这一段本来就被逐段计时过（上一轮用户卡了 729 秒、日志一片空白只能靠猜），但粒度不够。
2. **逐行插桩**：`_restore_write()` 给每个属性写一行 `restore: <组> -> <对象>.<属性> (was X, want Y)`。用户的第三次复现（07:37）直接把凶手钉死在这一行：
   ```
   [07:37:46] restore: engine -> RenderSettings.engine     ← 之后再无输出
   ```
3. **真凶**：原代码无条件 `setattr`，而用户场景的引擎**本来就是 CYCLES** → `render.engine = 'CYCLES'` 是**把同一个值赋给自己**。Blender 的 RNA setter 照做不误，触发一次引擎重建；这一下发生在**模态 operator 内部**（主线程被占），于是无限期不响应。同一段代码在 `--background` 里 0.0000 秒跑完 —— 这就是"后台怎么都复现不了、只有 GUI 卡"的原因。
4. **修法三条**：
   - 值一样 → 一个字节都不写（`skipped`）
   - 值真的不一样 → 排进推迟队列，由 `bpy.app.timers` 在 operator 退出后一帧补一条（界面始终能响应）
   - `--background` 没有事件循环、timer 永不触发 → `commit()` / `rollback()` 必须**同步排空**队列，否则引擎永不还原（GUI 不报错，只有测试会红）
5. 兜底：`capture()` 开始前先把上一轮遗留的推迟任务做掉，免得烘焙值被当成"用户原本的设置"快照进去。

### 验证
- 新增 `tests/mb_test_restore_guard.py`（18 项）：同值必须 `skipped`（用日志 + setattr 计数两条独立证据）、真变更必须推迟、background 必须同步排空、真烘一轮后引擎/采样/降噪/margin 全部还原。
- 在用户自己的 `Prop Creation.blend` 上真烘：4 任务 / 0 失败，收尾 `done in 0.00s`，`restore: engine -> ... skipped (already 'CYCLES')`。
- 全量 20 套 / 1303 项全绿。

### 结论
**GUI 专属卡死的根因是"还原代码无条件 setattr"**；任何"恢复设置"的循环都该先比再写，尤其别在模态里碰 `render.engine`。

### 给此次对话的总结
我给这个 bug 加了两轮工具：先逐段计时，再逐行计时。**这不是浪费** —— 两次都靠"卡死时最后一行日志"直接定位，没有靠猜（上一轮猜"是自动保存写盘"，护栏装上后照样卡）。教训是：**日志的粒度要跟"卡死点"的量级匹配**，20 行的黑箱就得拆成 20 行。

---

## 2026-09-25 · 修：被排除集合里的物体被当成烘焙目标（96 张图 92 张全废）✅

### 用户需求
用户把工程文件 `Prop Creation.blend` 放到工作区让我测（"你拿 test.blend 测完了再拿这个测测"）。这一条不是他报的，是我在复现卡死时顺带发现的。

### 修改过程
1. 真实烘焙该文件：`4 of 96 maps baked (92 failed)`，失败全都是
   `Object 'X' can't be selected because it is not in View Layer 'View Layer'`。
2. 探针查清现场：他文件里**几乎所有集合都是排除状态**（Outliner 里勾掉的），整个视图层只剩 `ShuiMa` 和 `Sun` 两个物体。而 `core/scene_scan.py::scan()` 走的是 `collection.objects`，**完全不看视图层**。
3. 修：新增 `objects_in_view_layer(view_layer)` + `NO_VIEW_LAYER` 常量，在 `scan()` 里把视图层外的物体挡在 `group.objects` 外并记进 `excluded`；`resolve_targets` / `scan_selected` 传 `context.view_layer`；`ui/session.refresh_groups` 与 `ui/panels` 两处也传，让第一页列表与计划一致。
4. 理由文案要点名是排除（不是含糊的 `No visible mesh`）并给行动指令：去 Outliner 把那个集合勾回来。

### 验证
- 同一个文件、同一套设置：**96 任务 → 4 任务；92 失败 → 0 失败**。
- 新增 `tests/mb_test_view_layer.py`（20 项），其中一条是**真的去 `select_set` 一下**（Blender 抛不抛异常才是判据，不是复读计划字段）。

### 结论
**"集合 → 一套贴图"的工作单位必须同时是"视图层里能选中的物体"**，否则整批任务在派发时才失败，且报错不可读。

### 给此次对话的总结
为了这一个功能我在 Blender 的视图层 API 上错了三次，全部实测记录在此（下次别再犯）：
1. `view_layer.objects` **不能当判据** —— 要等依赖图同步才更新，刚建完集合就编译计划时新建物体全被判掉（7 个套件一起变红）；要读 layer collection 树上的 `exclude`。
2. `scene.view_layers.active` 在 Blender 里**根本不存在**（AttributeError）——拿它当兜底会静默返回 None，把**所有**物体判掉。
3. 同一个 `collection` 在 layer collection 树里**可以出现多次**，`exclude` 必须"取或"，写成 `states[c] = exclude` 会被后一次覆盖（实测修完还剩 96 个任务）。
4. "没有视图层"和"视图层里没有"是两件事，前者必须 fail-open。

---

## 2026-09-25 · 安装方式踩坑（不是 bug，是环境）⚠

**Blender 只从 `%APPDATA%\...\addons\material_bakery` 加载插件**，工作树里的代码它看不见。更坑的是**同名模块会遮蔽 `sys.path`**：用 `blender --background <file> --python 探针.py` 时，探针里的 `from material_bakery...` import 到的是 APPDATA 那份旧代码，报出来的错看起来像代码 bug（例如 `has no attribute 'objects_in_view_layer'`）。

**规矩：拿工作树代码做无头验证，必须加 `--factory-startup`**（那样不加载用户插件，`sys.path` 才生效）。

---

## 协作规矩（用户 2026-09-25 亲口定，原话，任何会话都要遵守）

1. **请在我明确说"开始"之后才开始工作**
2. 使用 Git 管理项目，我会把 github 的 Personal Access Token 给你，**在我的明确说要创建 github 项目之后才创建项目上传**，在此前请只提交本地 git
3. 每次开发都默认我们在**"验证"阶段**，在这个阶段下，我给出的指示、指令都可能很宽泛，**多问我问题以敲定后续细节**
4. 如果有任何问题需要问我，**不要犹豫**，我希望能明确地给你达成目标的指令
5. **我给出任何反馈后不要立刻着手修改，问清楚我碰到的问题**
6. 保持工作目录整洁、易读；写给你自己用的（本次项目名字）.md 的时候有个大纲：把每次用户的回复分为：**用户需求 / 修改过程（修 bug・测试・交付・验证・结论）/ 给此次对话的总结**
7. **你的 .md 文件也会给其他对话读取**，读取的时候就能忽略前面已完成的工作，避免浪费上下文
8. **不准每次把反馈都写到用户看的 readme.md**，只有在用户要求的时候才从你的 md 里找重点写一份
9. 单次工作时间**超过 30 分钟**就给用户做一次阶段汇报

**Token 文件**：`DeepSeek Harness Git Token.txt`（已在 `.gitignore` 里，永不进版本库）。

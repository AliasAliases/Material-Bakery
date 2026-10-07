# 回归测试

针对 **DeepSeek's Material Bakery** 的无头自动化测试。全部在 `--background` 下跑，
不需要 GUI，也不会动你的工程文件。

## 一次跑完（推荐）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1
```

它把 `tests\*.py` 全部跑一遍、每套打一行结果，最后给一张汇总表（含每套耗时）。
两个需要"启动前准备"的套件（`mb_test_install.py` / `mb_test_zip_install.py`）
它会自己准备好隔离的配置目录与插件目录；zip 缺失时它还会先跑一遍 `build_zips.ps1`。

**当前状态：22 套 / 1629 项检查，0 失败、0 跳过**（Blender 4.5.13 LTS，
全量约 **31 秒**）。⚠ 项数取决于 `_samples/` 里有什么：这个仓库只带测试夹具
`_samples/test.blend`；如果本地还放着真机样本（`square_bush.blend` 等），
`mb_test_rebake.py` 会多跑一组曲面判据（**+24 项**），总数就是 1653。

工作区路径由脚本自身位置推导（`WS` = 本文件所在目录的上一级），
所以 clone 到任何目录都能直接跑 —— 不需要改任何路径。

### 三种结果，别混在一起看

| 状态 | 含义 |
|---|---|
| `PASS` | 打印了 `ALL CHECKS PASSED` |
| `SKIP` | 套件自己声明 `SKIPPED (原因)` —— **不是失败**，比如某个可选的外部依赖不在。汇总里会单独列出原因 |
| `FAIL` | 打印了 `FAILED n check(s):` 加具体失败项 |

跳过必须能被区分出来：如果"引擎不在"也算红，久而久之所有人都会无视红色。
所以跑绿的运行结束时会顺手删掉 `_probe\install` 与 `_probe\zipinstall`
（约 108 MB 的临时插件副本）；跑挂了就**保留现场**给你看。加 `-KeepProbe` 可强制保留。

## 单独跑一套

```powershell
$blender = "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
& $blender --background --factory-startup --python "tests\mb_test_stability.py"
```

脚本最后会打印 `ALL CHECKS PASSED` 或 `FAILED n check(s):` 加具体失败项。

只跑一部分：`-Filter "mb_test_*"`（或 `-Filter mb_smoke.py`）。

## 各套覆盖什么

| 脚本 | 项 | 覆盖内容 |
|---|---|---|
| `mb_test_core.py` | 140 | 类型注册表（46 种）、命名策略、材质仓库复用、材质槽交付（逐面索引备份/还原、单槽捷径、面数变了要退回） |
| `mb_test_wizard.py` | 149 | 五页向导全流程、严格顺序导航、烘焙列表增删移清去重、**静态 AST 审计**（panel 引用的 operator 存在、bl_idname 唯一、timer 路径零 IDProperty 写入）；**实施轮 1.6**：烘完落盘的文件名就是 `image_name`（没有 `_NoAlpha` 之类的注明）、没勾 alpha 图时 base_color 是 24 位 |
| `mb_test_reported_issues.py` | 140 | 用户报过的问题的回归：输出子文件夹、暂停/停止、准备跟着任务走、进度全程在动、按钮真的画出来、模态驱动、内存估算按实测斜率、`No active image found` 不点名、看门狗超时后收尾 |
| `mb_test_presets_udim.py` | 120 | UDIM 瓦片识别与逐瓦片真机烘焙 + UV 精确还原；预设存取与版本迁移；暂停/取消；抗锯齿；自适应边距；报告落盘 |
| `mb_test_stability.py` | 95 | 运行期偏好护栏（借/还往返、每条退出路径）、阶段计时落盘、逐面索引读写、Hide Unrelated 的藏与放、采样/AO 提醒、写文件留痕 |
| `mb_test_engine.py` | 91 | 三种目标模式扫描、计划编译与命名、事务还原、UV 重叠预检、`BakeJob` 状态机（NullBackend）、单任务失败不终止整批、取消、重跑复用图像 |
| `mb_test_panel_draw.py` | 165 | **每个工作流 × 每一步各真画一遍**（Bake 五页 + Re-bake 五页，假 layout）：任何一页 `draw()` 抛异常都要抓出来、每页必须有导航按钮、**icon 名必须在该 Blender 版本合法列表里**、Checklist 每一行都真的画出来、Re-bake 首页把分辨率来源画出来；**实施轮 1.1**：Result 页不再画 Compare、收尾文案写明不可逆、UV Sets 新标签与物体计数；**实施轮 1.2**：`None` 哨兵可选项、两侧各写各的说明、Result 页"预览只是临时"、默认模式下法线例外的说明；**实施轮 1.4**：Result 页画出合并 alpha 的勾选框（有/无 Alpha 两种状态；那条"同一个设置属性不会画两遍"的断言原来是空转的，`is` 改 `==` 之后才真的在查）；**实施轮 1.6**：那个带 alpha 的导出按钮**删掉**了（默认那张本来就带 A）、页面上不再有 "alpha" 字样 |
| `mb_test_rebake.py` | 154 | **Re-bake（跨 UV 重烘）**：两个下拉与两条步骤表（数据驱动，`None` 哨兵 + 旧哨兵兼容）、合成平面判读（注入 -> 读到源层坐标 0.2051 / 对照 -> 写层坐标 0.807）、零残留（含**派发中途失败**那一路）、边界（缺层不注入、`From == To` 不注入、`Mapping` 钉在之前）、工作流隔离、**曲面真机 `BushSquare` 的 `base_color` NATIVE / EMISSION 两档**（MAE 0.0018 vs 0.2002、命中 99.94%）；**实施轮 1.1 的交付/预览**：预览连渲染层一起切、收尾真删旧层并改名 `UVMap`（保留层 UV 落在 0.7–0.9 证伪"删反"）、被材质引用的层不删、副本成品化而原物体逐项不变、Remove Baked Slot 只还原材质槽（UV 层回不来）、Bake 流程预览一个字不动 |
| `mb_test_round32.py` | 100 | **实施轮 1.2**：①**名字与现取** —— 计划里保留名字、用户那条路径（建计划 → 进编辑模式 → 出来 → Preview）不抛异常、数据块被换掉（旧引用作废 + 同名重建）之后交付/预览/收尾/建副本照常、真没了的物体只记 `object is gone`；②**UV Sets 下拉** = `None` + 实际检测到的层名，旧哨兵当 `None`，`layer_stats` 的 nothing / same / no_pin 三个标志；③**法线通道** —— 静态判据 + **真机三档比对**（Bake 与 Re-bake 两条工作流各一遍：带链 / 断开链的几何基准；岛内平均偏移 0.19、99% 像素偏离，基准 < 1e-4），`standard.normal` 与 `shader.normal` 逐像素等价（0.000000），值通道仍走手术 |
| `mb_test_rescue.py` | 85 | 从磁盘扫回贴图（强杀之后的救援路径）：manifest 优先、文件名回退、三套命名世代、扫完即"烘完的界面"；**实施轮 1.5b**：`_NoAlpha` 注明要被剥掉再解析（不剥的话 `NoAlpha` 以 `Alpha` 结尾会被认成 `shader.alpha`） |
| `mb_test_surgery.py` | 67 | 节点手术：把要烘的信号接到 Emission、烘完精确还原（节点集合/连线/常量逐项比对） |
| `mb_test_s2a.py` | 52 | 专属参数接线（通道打包 R/G/B、AO、UV 层名、颜色属性、法线空间真的传到引擎）+ Selected to Active 投影真采到高模（像素证据） |
| `mb_test_real_bake.py` | 55 | **真机 Cycles 烘焙** `_samples/test.blend` 的 3 个物体共 9 张图：出图有内容、渲染设置与材质节点完全还原、导出扩展名与所选格式一致、重跑覆盖同名文件；外加真机非正方形（512×256）；**实施轮 1.6**：烘完落盘的文件名就是 `image_name`（不带任何后缀） |
| `mb_test_zip_install.py` | 20 | **装 zip**：条目名必须正斜杠、根目录是 `material_bakery/`、没有 `__pycache__`；再用 `addon_install` 真装一遍 + 启用 + 验目录结构 + 卸载 |
| `mb_smoke.py` | 26 | 跨版本冒烟：注册、类型表、9 类真机烘焙、像素正确性、导出与重复导出、UI、注销。3.6.23 / 4.5.13 / 5.2.1 上都过 |
| `mb_test_parity.py` | 21 | **逐像素对照**：新版 vs **独立手写**的参考烘焙（不复用本插件任何代码）。`base_color` / `combined` 完全一致（max 0.0000），数据贴图只有位深量化差 |
| `mb_test_view_layer.py` | 20 | **被排除集合不能当烘焙目标**：视图层外的物体不进计划、理由点名"集合被排除"、计划里每个目标都真的能 `select_set` 选中；含嵌套排除 |
| `mb_test_install.py` | 18 | 打包好的那份能不能装：走正规 `addon_enable`，验加载路径、版本、向导属性、真机烘焙、禁用（需隔离配置目录） |
| `mb_test_restore_guard.py` | 18 | **收尾还原的三条规矩**：值一样一个字节都不写（日志 `skipped` + setattr 计数两条证据）、真变更排进推迟队列、background 必须同步排空；真烘一轮后引擎/采样/降噪/margin 全部还原 |
| `mb_test_timeout_guard.py` | 42 | **实施轮 1.3「卡死」防线**：①**证据保鲜** —— 每轮一个按时间戳命名的日志目录、第二轮一个字节都不覆盖第一轮、跑到一半磁盘上就有内容、指路纸条指向最新一轮；②**尾巴分段** —— 五段都有 start/done、`unclosed() == []`、尾巴 < 10s（卡死会在这里红）、推迟队列排空、最后落到 `session tail complete`；③**看门狗**（背景不注册 / 回调自测）；④**同值写入**（引擎同值跳过、每张任务的位深第一张写第二张跳、`write_setting` 返回值） |
| `mb_test_base_alpha.py` | 51 | **base_color 默认就带 alpha（实施轮 1.6）**：①**建图时机** —— 该组有 `shader.alpha` 任务时 base_color 目标图 4 通道（depth 32），没有则 24 位；②**合并读数** —— 落盘那张 `depth == 32`、**A == alpha 图的灰度**（量化容差）、**RGB 与"没勾 alpha"那轮逐像素相同**（max diff 0.000000）、文件名就是原名；③**顺序** —— alpha 排在 base_color **前 / 后**两种顺序都要对（含 manifest 两条都在）；④**边界** —— alpha 任务失败 / 尺寸不一致 → base_color 照样按原名落盘（A 恒 1.0 不透明）+ 报告点名；⑤**交付材质** —— base_color 自带 alpha 时它的 Alpha 输出接到 Principled；⑥复用同一张图时通道数变了要**重建**；⑦老名字 `…_NoAlpha.png` 仍能解析回 base_color |
| `mb_test_export.py` | 36 | **导出层**：①**清单** —— `build_manifest` 的三来源（真烘过用报告 / 扫回来的贴图 / 重开文件后还在的持久列表）、失败条目与空行不进清单；②**救援状态导出**（1.5 修的那个崩溃）—— 没有 `job`、**没有 `report`** 时两个导出入口都能出图、不抛异常；③**空状态** —— 给明确错误而不是 `AttributeError`；④**清单与逐张勾选没有交集 → 点名报错**（1.6 顺手修的"导 0 张且不报错"）；⑤**回归** —— 产物与"Blender 自己 `save()` 的副本"逐字节一致、文件名就是 `image_name`；⑥alpha 旧机制（勾选框 / 按钮 / 后缀 / 合并函数）真的没了 |

## 夹具与临时文件

- **`_samples/test.blend` 是测试夹具，不是垃圾**（2026-09-25 从工作区根目录搬进 `_samples/`）：
  `mb_test_real_bake.py` 与 `tools/measure_bake_memory.py` 都会打开它跑真实的 Cycles 烘焙
  （设计文档第 11 / 20 节的实测数字就是从它来的）。`.gitignore` 专门为它开了 re-include
  例外，所以它随版本库走、clone 下来就能跑；`_samples/` 里其它样本仍然忽略。
- 各套的产物写在 `_probe\<套件名>\` 下，每次运行前自动清空；`_probe\install` 与
  `_probe\zipinstall` 是"装插件"用的隔离环境（各含一整份插件副本，共约 108 MB），
  跑绿时由 runner 自动删除。
- **插件自己的运行日志**（实施轮 1.3 起）：`%TEMP%\material_bakery_logs\<yyyyMMdd-HHmmss>\material_bakery.log`
  —— **每轮一个目录，任何一次运行都不覆盖上一次**，每行 `flush + fsync`（强杀之后最后一行还在）。
  旧的稳定路径 `%TEMP%\material_bakery_last_run.log` 现在只是一张**指路纸条**（写着最新一轮在哪）。
  探针的读数落在 `_probe\r33_*.txt`（`_probe\probe_r33_tail.py` 是收尾计时探针，
  `_probe\run_r33_matrix.ps1` 是带外部超时的对照矩阵驱动）。

## 两个需要"启动前准备"的套件

插件的搜索路径是 Blender **启动时**扫描的，所以这两套必须在启动前把目录放好
（`run_all_tests.ps1` 已经替你做了，手动跑时照下面来）：

```powershell
# 装文件夹（mb_test_install.py）
$probe = "$ws\_probe\install"
New-Item -ItemType Directory -Force -Path "$probe\scr\addons" | Out-Null
Copy-Item -Recurse "$ws\Material-Bakery-Dev-Env\material_bakery" "$probe\scr\addons\material_bakery"
$env:BLENDER_USER_CONFIG  = "$probe\cfg"
$env:BLENDER_USER_SCRIPTS = "$probe\scr"

# 装 zip（mb_test_zip_install.py）—— 只要一个干净的空 scripts 目录，它自己会装
```

## 不是测试，是量尺

- `tools/measure_bake_memory.py` —— 用 Windows 的 `K32GetProcessMemoryInfo` 拿**真实**
  工作集，并真机烘一轮量每张图的实际成本。注意 `bpy.data.images.new()` 是惰性的：
  建 96 张 2048² 图只涨 0.02 GB，内存估算的斜率（约 18 字节/像素）是从这里量出来的。
- `tools/probe_run_prefs.py` / `probe_undo_toggle.py` —— 偏好设置 API 的名字、
  `save_pre` 参数、`foreach_get` 与 Python 循环的真实耗时，以及
  "来回切 `use_global_undo` 会不会清空撤销历史"（结论：不会）。

## 两个踩过的坑（下次先怀疑断言）

1. **`hasattr(bpy.ops.xxx, 任意名字)` 永远返回 `True`**（ops 命名空间懒加载）。
   判断操作符是否存在必须用 `dir()`；测试里的 `op_exists()` 就是干这个的，并带自检断言。
2. **断言写反比代码出错更常见**。多次出现"代码正确、断言写错"：例如默认立方体的 UV
   只占 0.375 面积，所以"完全重叠"的正确判据是 `overlap == covered` 而不是
   `overlap > 0.9`。看到 `[FAIL]` 先怀疑断言本身。

> ⚠ `tools\*.ps1` 请保持 **纯 ASCII**：Windows PowerShell 5.1 在没有 BOM 时会按 ANSI
> 读 `.ps1`，中文注释会把解析器搞坏（报的是莫名其妙的 `Unexpected token '}'`）。

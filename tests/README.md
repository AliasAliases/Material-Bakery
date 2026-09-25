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

**当前状态：20 套 / 1303 项检查，0 失败、0 跳过**（Blender 4.5.13 LTS，
全量约 **31 秒**）。公开版（无 Remesh 页）是 **17 套 / 1186 项**。

工作区路径由脚本自身位置推导（`WS` = 本文件所在目录的上一级），
所以 clone 到任何目录都能直接跑 —— 不需要改任何路径。

### 三种结果，别混在一起看

| 状态 | 含义 |
|---|---|
| `PASS` | 打印了 `ALL CHECKS PASSED` |
| `SKIP` | 套件自己声明 `SKIPPED (原因)` —— **不是失败**，比如没装 QuadRemesher 引擎。汇总里会单独列出原因 |
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
| `mb_test_wizard.py` | 145 | 五页向导全流程、严格顺序导航、烘焙列表增删移清去重、**静态 AST 审计**（panel 引用的 operator 存在、bl_idname 唯一、timer 路径零 IDProperty 写入） |
| `mb_test_reported_issues.py` | 140 | 用户报过的问题的回归：输出子文件夹、暂停/停止、准备跟着任务走、进度全程在动、按钮真的画出来、模态驱动、内存估算按实测斜率、`No active image found` 不点名、看门狗超时后收尾 |
| `mb_test_presets_udim.py` | 120 | UDIM 瓦片识别与逐瓦片真机烘焙 + UV 精确还原；预设存取与版本迁移；暂停/取消；抗锯齿；自适应边距；报告落盘 |
| `mb_test_stability.py` | 95 | 运行期偏好护栏（借/还往返、每条退出路径）、阶段计时落盘、逐面索引读写、Hide Unrelated 的藏与放、采样/AO 提醒、写文件留痕 |
| `mb_test_engine.py` | 91 | 三种目标模式扫描、计划编译与命名、事务还原、UV 重叠预检、`BakeJob` 状态机（NullBackend）、单任务失败不终止整批、取消、重跑复用图像 |
| `mb_test_panel_draw.py` | 88 | 五页各真画一遍（假 layout）：任何一页 `draw()` 抛异常都要抓出来、每页必须有导航按钮、**icon 名必须在该 Blender 版本合法列表里**、Checklist 每一行都真的画出来 |
| `mb_test_rescue.py` | 79 | 从磁盘扫回贴图（强杀之后的救援路径）：manifest 优先、文件名回退、三套命名世代、扫完即"烘完的界面" |
| `mb_test_surgery.py` | 67 | 节点手术：把要烘的信号接到 Emission、烘完精确还原（节点集合/连线/常量逐项比对） |
| `mb_test_s2a.py` | 52 | 专属参数接线（通道打包 R/G/B、AO、UV 层名、颜色属性、法线空间真的传到引擎）+ Selected to Active 投影真采到高模（像素证据） |
| `mb_test_remesh_page.py` | 45 | Remesh 页：5 页顺序、`<原名>_lowpoly` 改名（重名保护、关掉改名）、provider 认前后缀、没装时安全降级、`RemeshJob` 状态机 |
| `mb_test_real_bake.py` | 53 | **真机 Cycles 烘焙** `test.blend` 的 3 个物体共 9 张图：出图有内容、渲染设置与材质节点完全还原、导出扩展名与所选格式一致、重跑覆盖同名文件；外加真机非正方形（512×256） |
| `mb_test_quadremesher.py` | 34 | QuadRemesher 接入：`Retopo_*` 配对、provider 选目标/源、活动物体优先、配对不上退回隐藏物体、缺 UV 提醒。**不需要真装 QuadRemesher** |
| `mb_test_zip_install.py` | 28 | **装 zip**：条目名必须正斜杠、根目录是 `material_bakery/`、没有 `__pycache__`；再用 `addon_install` 真装一遍 + 启用 + 验目录结构 + 卸载；顺带验 QuadRemesher 那个 40 MB 的 zip |
| `mb_smoke.py` | 26 | 跨版本冒烟：注册、类型表、9 类真机烘焙、像素正确性、导出与重复导出、UI、注销。3.6.23 / 4.5.13 / 5.2.1 上都过 |
| `mb_test_quadremesher_real.py` | 23 | **真机端到端**：真的跑一遍 QuadRemesher 引擎，验证产物改名、原对象被隐藏、产物没有 UV，再走完整流水线投影烘焙并检查像素采到高模的颜色 |
| `mb_test_parity.py` | 21 | **逐像素对照**：新版 vs **独立手写**的参考烘焙（不复用本插件任何代码）。`base_color` / `combined` 完全一致（max 0.0000），数据贴图只有位深量化差 |
| `mb_test_view_layer.py` | 20 | **被排除集合不能当烘焙目标**：视图层外的物体不进计划、理由点名"集合被排除"、计划里每个目标都真的能 `select_set` 选中；含嵌套排除 |
| `mb_test_install.py` | 18 | 打包好的那份能不能装：走正规 `addon_enable`，验加载路径、版本、向导属性、真机烘焙、禁用（需隔离配置目录） |
| `mb_test_restore_guard.py` | 18 | **收尾还原的三条规矩**：值一样一个字节都不写（日志 `skipped` + setattr 计数两条证据）、真变更排进推迟队列、background 必须同步排空；真烘一轮后引擎/采样/降噪/margin 全部还原 |

## 夹具与临时文件

- **`test.blend`（工作区根目录）是测试夹具，不是垃圾**：`mb_test_real_bake.py` 与
  `tools/measure_bake_memory.py` 都会打开它跑真实的 Cycles 烘焙
  （设计文档第 11 / 20 节的实测数字就是从它来的）。
- 各套的产物写在 `_probe\<套件名>\` 下，每次运行前自动清空；`_probe\install` 与
  `_probe\zipinstall` 是"装插件"用的隔离环境（各含一整份插件副本，共约 108 MB），
  跑绿时由 runner 自动删除。

## 两个需要"启动前准备"的套件

插件的搜索路径是 Blender **启动时**扫描的，所以这两套必须在启动前把目录放好
（`run_all_tests.ps1` 已经替你做了，手动跑时照下面来）：

```powershell
# 装文件夹（mb_test_install.py）
$probe = "$ws\_probe\install"
New-Item -ItemType Directory -Force -Path "$probe\scr\addons" | Out-Null
Copy-Item -Recurse "$ws\MaterialBakery\material_bakery" "$probe\scr\addons\material_bakery"
$env:BLENDER_USER_CONFIG  = "$probe\cfg"
$env:BLENDER_USER_SCRIPTS = "$probe\scr"

# 装 zip（mb_test_zip_install.py）—— 只要一个干净的空 scripts 目录，它自己会装
```

`mb_test_quadremesher_real.py` 需要 QuadRemesher 在隔离环境里启用
（把 `MaterialBakery\QuadRemesher` 复制进 `_probe\...\scr\addons\`），
而且顺序必须是「先 `read_factory_settings`、再 `addon_enable`」——
反过来的话 factory reset 会把刚注册的 `scene.qremesher` 弄没。
**没引擎 / 没许可证时会明确 SKIP，不会变成失败。**

## 不是测试，是量尺

- `tools/measure_bake_memory.py` —— 用 Windows 的 `K32GetProcessMemoryInfo` 拿**真实**
  工作集，并真机烘一轮量每张图的实际成本。注意 `bpy.data.images.new()` 是惰性的：
  建 96 张 2048² 图只涨 0.02 GB，内存估算的斜率（约 18 字节/像素）是从这里量出来的。
- `tools/probe_run_prefs.py` / `probe_undo_toggle.py` —— 偏好设置 API 的名字、
  `save_pre` 参数、`foreach_get` 与 Python 循环的真实耗时，以及
  "来回切 `use_global_undo` 会不会清空撤销历史"（结论：不会）。
- `tools/qr_probe_state.py` / `qr_check_coverage.py` —— QuadRemesher 跑完之后
  场景到底是什么状态、投影覆盖了多少。接入方案全靠那几个实测事实。

## 两个踩过的坑（下次先怀疑断言）

1. **`hasattr(bpy.ops.xxx, 任意名字)` 永远返回 `True`**（ops 命名空间懒加载）。
   判断操作符是否存在必须用 `dir()`；测试里的 `op_exists()` 就是干这个的，并带自检断言。
2. **断言写反比代码出错更常见**。多次出现"代码正确、断言写错"：例如默认立方体的 UV
   只占 0.375 面积，所以"完全重叠"的正确判据是 `overlap == covered` 而不是
   `overlap > 0.9`。看到 `[FAIL]` 先怀疑断言本身。

> ⚠ `tools\*.ps1` 请保持 **纯 ASCII**：Windows PowerShell 5.1 在没有 BOM 时会按 ANSI
> 读 `.ps1`，中文注释会把解析器搞坏（报的是莫名其妙的 `Unexpected token '}'`）。

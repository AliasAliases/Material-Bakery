# Material Bakery

Step-by-step texture baking for Blender 4.x / 5.x, with **baking and delivery kept apart**. Most bake add-ons are fire-and-forget: they bake, hand you the images, and that is the end of the interaction. Here, baking produces images and a result page, and only then do you decide what to do with them — export the textures, build one material per collection, adjust the material slots, or duplicate the objects into a final collection. If the result is not what you wanted, the next attempt costs you a click, not another full bake.

This repository is the **standalone build**. The private build of the same add-on has an extra fifth wizard page that drives a third-party retopology engine (QuadRemesher); that page is not part of this build at all, so this one has four wizard pages and no retopology feature.

## What it does

One material and one texture set per collection. A collection named `Common Parts 1` produces a material of the same name and maps such as `Common Parts 1-BaseColor-2k.png`, `-Normal`, `-Metallic` and `-Roughness`, shared by every object in the collection.

The bake list holds **46 bake types** — Principled BSDF inputs (base color, metallic, roughness, IOR, specular, coat, sheen, subsurface, emission, alpha…), the standard Cycles passes (combined, AO, shadow, glossy, position), channel packing, UV and color-attribute maps, pointiness and displacement — each with its own parameters, such as the R/G/B slots of a packed map, the AO distance, or the normal space. UDIM tiles are detected and baked tile by tile. Selected-to-Active projection bakes a low-poly target from a high-poly source within the same run.

Sample counts are per channel group, because the two halves of a bake list do not want the same number. Channels whose value is the same in every sample (color, roughness, metallic, normal, …) default to **1 sample** — baking them 64 times renders the same image 64 times. Channels that really take samples, AO and shadow among them, default to **64**, because fewer shows up as noise. Both groups are selectable from Low/Medium/High, and the two numeric boxes switch their group to Custom as soon as you type in them.

## Your UVs are never touched

Objects are baked through the UV layers they already have. The add-on does not pack UVs, does not add a UV layer and does not re-project anything, so UV islands that you placed on top of each other on purpose — mirrored parts sharing one piece of texture, for instance — stay overlapping and keep sharing it. The cost of that choice is stated plainly: if two overlapping islands need different content, the later bake overwrites the earlier one. The overlap percentage is still shown on the first page, as information rather than as a gate.

A layer called `MBAKERY_UV` exists in exactly one situation: you ask for it. On the Settings page, `Unwrap With SmartUV` (off by default) can unwrap objects that have no UVs at all, and `Unwrap Into` chooses whether that new unwrap goes into the mesh's normal UV layer or into a separate `MBAKERY_UV` layer. Objects that already have UVs are left alone either way.

## Delivery is non-destructive and reversible

When a run finishes, the baked material is **appended** to each object as an extra slot. The original slots, their order and their per-face assignment are untouched, so the object still looks exactly as it did. From there:

- **Add Slot** attaches the baked material where it is missing; **Preview Baked** assigns the faces to it, which is what actually changes the look — adding the slot alone does not. **Show Original** puts the faces back on the original material.
- **Keep Only Baked (Export Ready)** deletes every slot except the baked one and moves all face indices onto it, so an exporter sees a single clean material.
- **Restore Original Slots** rebuilds the original slots and their per-face indices from the backup written onto each object at delivery time. Finalize is reversible the same way.

Only objects whose every requested map succeeded are touched; one failed map leaves that object's material alone. An object with no material at all is named in a warning, because Blender rejects the whole selection with `No active image found` in that case.

## Install

```powershell
# mirror the working tree into the shipping folder, then build the zip
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_package.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_zips.ps1
```

Install `MaterialBakery\material_bakery_install.zip` through `Edit ▸ Preferences ▸ Add-ons ▸ Install…`, enable **Material Bakery**, then press `N` in the 3D viewport and pick the `Material Bakery` tab in the sidebar.

**Fully restart Blender afterwards.** `Install…` only writes files to disk; the copy Blender already has loaded in memory is not replaced, so a fix can be sitting on disk while the old code keeps running. Page 1 shows `v<version> · build <timestamp>` so you can tell which build is actually loaded.

## The workflow

1. **Targets & Maps** — pick the collections, a single collection or the selected objects, and the maps to bake, with a per-type row for the map's own parameters.
2. **Settings** — naming scheme, output folder and format, bake quality, margin, antialiasing, UDIM, UV preparation for objects without UVs, and high-poly projection.
3. **Bake** — the queue with the current map, live per-map timings, and `Pause` / `Resume` / `Stop` on their own row. Blender stays usable throughout, and `ESC` cancels natively.
4. **Result** — texture list grouped by collection, material building, the slot operations above, final object duplication, and a JSON report.

## Two things that used to hurt

Global undo and auto-save are paused for the duration of a run and restored on every exit path — finished, cancelled, failed, add-on unregistered, file changed. A multi-gigabyte auto-save write immediately after the last map is what makes Blender *look* hung: the memory stays flat and the interface does not move, which is a disk write, not a calculation. Both switches are put back exactly as they were found, and nothing else in the preferences is modified.

Every phase of a run writes a timed log line, and every file write is recorded by a save handler as well, so a stall is never a black box:

```
[wizard +421.31s | +10.24s] dispatch Baked-BaseColor-2048: done in 10.24s
[wizard +731.55s | +310.24s] file write done in 309.80s: ...\scene.blend
```

The first number is the time since the run started, the second the time since the previous line, so the slow segment is visible at a glance. The log is flushed to disk line by line (`material_bakery_last_run.log` in the system temporary directory) and survives Blender being killed; the Bake and Result pages can open it, and errors and warnings are written to it too.

## Repository layout

| Path | What |
|---|---|
| `material_bakery/` | Working tree — edit here |
| `MaterialBakery/material_bakery/` | Shipping copy — what the zip is built from |
| `MaterialBakery/README.md` | User manual: behaviour, edge cases, known limits |
| `MATERIAL_BAKERY_DESIGN.md` | Design notes and the measured numbers behind the defaults |
| `tests/` | Headless regression suites — see `tests/README.md` for what each one covers |
| `tools/` | Packaging, sync, test-runner and measurement scripts |

## Tests

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1
```

Every suite runs headless, prints one result line, and the run ends with a summary table of per-suite timings. A suite may declare itself `SKIPPED (reason)`, which is not a failure and is listed separately, so a skip is never mistaken for a green light. Paths are derived from each script's own location, so a clone runs from any directory. `tests/README.md` maps each suite to what it covers.

## Licence

GPL v2 or later — the full text is in `MaterialBakery/LICENSE.txt`. No third-party binaries are bundled with this build.

---

# Material Bakery（中文）

Blender 4.x / 5.x 的**分步烘焙**插件，把**烘焙**和**交付**分成两件事。多数烘焙插件是"一键烘完就结束"：出图，然后交互到此为止。这里烘焙产出的是贴图和一张结果页，之后才由你决定怎么用 —— 导出贴图、按集合建材质、调整材质槽、把最终物体复制到一个集合里。结果不满意，下一次尝试的代价是一次点击，而不是重跑一整轮烘焙。

本仓库是**独立构建版**。同一插件的私有构建里多出第 5 页，用来驱动第三方重拓补引擎（QuadRemesher）；那一页在本构建里完全不存在，所以这里是 **4 页向导，也不含任何重拓补功能**。

## 它做什么

一个集合一张贴图、一个材质。集合 `Common Parts 1` 会得到同名材质和 `Common Parts 1-BaseColor-2k.png`、`-Normal`、`-Metallic`、`-Roughness` 这一套贴图，集合里所有物体共用。

烘焙列表里有 **46 种烘焙类型** —— Principled BSDF 的各项输入（基础色、金属度、粗糙度、IOR、高光、涂层、光泽、次表面、自发光、Alpha 等）、Cycles 的标准通道（Combined、AO、阴影、光泽、位置）、通道打包、UV 与颜色属性贴图、Pointiness、置换 —— 每种类型下面都有自己的参数，例如打包图的 R/G/B 通道、AO 距离、法线空间。UDIM 会自动识别并逐瓦片烘焙，Selected-to-Active 投影可以在同一轮里用高模烘低模。

**采样数按通道分两组**，因为一张烘焙列表的两半要的采样数并不一样。每个采样算出来都是同一个值的通道（颜色、粗糙度、金属度、法线……）默认 **1 个采样** —— 烘 64 遍就是把同一张图算 64 遍；真正需要采样的通道（AO、阴影等）默认 **64 个采样**，少了就是噪点。两组都可以在低/中/高之间切换，两个数字输入框一旦被改动，所在组会自动切到 Custom。

## 绝不动你的 UV

烘焙走的是物体现有的 UV 层：不打包、不新建 UV 层、不做任何重投影。所以你**故意**让 UV 岛互相重叠的做法（比如镜像件共用一块纹理）会被原样保留，重叠的岛继续共用那块纹理。这个选择的代价如实写在这里：两块重叠区域如果需要的内容不同，**后烘的会盖掉先烘的**。第 1 页仍然显示重叠百分比，它只是信息，不拦你。

名为 `MBAKERY_UV` 的层只在一个前提下出现：**你自己要它**。第 3 页的 `Unwrap With SmartUV`（默认关）可以给完全没有 UV 的物体展 UV，`Unwrap Into` 决定展进物体本来那层还是单独一层 `MBAKERY_UV`。本来就有 UV 的物体两种情况都不会被改动。

## 交付是非破坏性、可逆的

一轮跑完，烘焙材质是**追加**到每个物体上的一个新槽。原来的槽、它们的顺序、逐面的材质分配都没有被碰过，所以物体外观和烘焙前完全一样。接下来：

- **Add Slot** 把烘焙材质挂到还没有它的物体上；**Preview Baked** 把面分配到该槽 —— 真正改变外观的是这一步，只加槽不改外观。**Show Original** 把面切回原材质。
- **Keep Only Baked (Export Ready)** 删掉除烘焙槽以外的所有槽并把面索引归 0，导出时看到的就是一个干净材质。
- **Restore Original Slots** 用交付时写在每个物体上的备份把原槽和逐面索引重建回来，所以收尾这一步同样可逆。

只有全部烘焙项都成功的物体才会被改动，有一个类型失败就不会拿它盖掉原材质。一个材质都没有的物体会被点名警告：那种情况下 Blender 会用 `No active image found` 整批拒绝，且不说是哪个物体。

## 安装

```powershell
# 先把工作树同步进发布目录，再打 zip
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_package.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_zips.ps1
```

通过 `Edit ▸ Preferences ▸ Add-ons ▸ Install…` 安装 `MaterialBakery\material_bakery_install.zip`，启用 **Material Bakery**，然后在 3D 视图按 `N`，在侧栏里选 `Material Bakery` 标签页。

**装完请完全退出 Blender 再启动。** `Install…` 只是把文件写到磁盘上，Blender 内存里已经加载的那份不会被替换 —— 于是磁盘上修好了、跑的还是旧代码。第 1 页会显示 `v<版本> · build <时间戳>`，就是为了让你一眼看出真正加载的是哪一版。

## 4 步工作流

1. **Targets & Maps** —— 选目标（按集合 / 单个集合 / 选中的物体）和要烘的贴图，每种类型有自己的一行参数。
2. **Settings** —— 命名方案、输出目录与格式、烘焙质量、边距、抗锯齿、UDIM、给没有 UV 的物体做 UV 准备、高模投影。
3. **Bake** —— 队列、当前贴图、逐张实时耗时，`Pause` / `Resume` / `Stop` 单独一行。过程中 Blender 照常可用，`ESC` 原生就能取消。
4. **Result** —— 按集合分组的贴图列表、建材质、上面的槽操作、复制最终物体、导出 JSON 报告。

## 曾经真正让人难受的两件事

**全局撤销和自动保存在一轮烘焙期间被暂停，并在每一条退出路径上还原** —— 跑完、取消、失败、注销插件、换文件都算。最后一张图烘完之后紧接着的那次多 GB 自动保存写入，正是 Blender "看起来卡死"的来源：内存是平的、界面不动，那是磁盘写入而不是计算。两个开关按原样借还，偏好设置里其它任何东西都不改。

**一轮里的每个阶段都写一条带耗时的日志，任何文件写入也会被记录**，所以卡顿不是黑箱：

```
[wizard +421.31s | +10.24s] dispatch Baked-BaseColor-2048: done in 10.24s
[wizard +731.55s | +310.24s] file write done in 309.80s: ...\scene.blend
```

左边一个数是距本轮开始，右边一个数是距上一条，谁慢一眼就看出来。日志逐行 flush 到系统临时目录下的 `material_bakery_last_run.log`，强杀也不丢；Bake 页和 Result 页可以直接打开它，错误和警告同样会写进去。

## 仓库结构

| 路径 | 是什么 |
|---|---|
| `material_bakery/` | 工作树 —— 在这里改 |
| `MaterialBakery/material_bakery/` | 发布副本 —— zip 就是从这里打的 |
| `MaterialBakery/README.md` | 用户手册：行为、边界情况、已知限制 |
| `MATERIAL_BAKERY_DESIGN.md` | 设计说明，以及各项默认值背后的实测数字 |
| `tests/` | 无头回归测试 —— 各套覆盖什么见 `tests/README.md` |
| `tools/` | 打包、同步、测试运行器与测量脚本 |

## 测试

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1
```

所有套件都在无头模式下运行，每套打一行结果，最后给一张含每套耗时的汇总表。套件可以自己声明 `SKIPPED (原因)` —— 那不是失败，会单独列出，免得"跳过"被当成绿灯。路径由脚本自身位置推导，克隆到任何目录都能直接跑。每套具体覆盖什么，见 `tests/README.md`。

## 许可

GPL v2 或更高版本，全文见 `MaterialBakery/LICENSE.txt`。本构建不附带任何第三方二进制文件。

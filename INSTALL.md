# 安装说明 —— DeepSeek's Material Bakery

## 装哪一个

```
material_bakery_install.zip      ← 插件本体（0.18 MB）
```

两个都在工作区根目录，也在 `MaterialBakery\` 里各有一份。

1. Blender → `Edit` → `Preferences` → `Add-ons` → 右上角 `Install...`
2. 选 `material_bakery_install.zip`
3. 在列表里搜 `Material Bakery`，勾选启用
4. 3D 视图按 `N` → `Material Bakery` 页签
5. 第 1 页顶上会写版本号 + 打包时间戳，例如 `v1.0.3 · build 2026-09-21 04:08`

## ⚠ 装完必须完全退出 Blender 再启动

`Install...` 只是把文件放到磁盘上，**Blender 内存里那份旧模块不会被换掉**。
只重开 `.blend` 是没用的，插件还是旧代码。

所以先看一眼第 1 页那个 `v版本号 · build 时间戳`：
**没变成你刚装的那一版，就说明你还在跑旧代码。** 这一条我们已经因此浪费过一整轮排查。

## 卸载 / 换版本

`Preferences` → `Add-ons` → 搜 `Material Bakery` → 取消勾选 → 右侧 `Remove`。
插件只往场景里写这些标记，删掉插件它们会留在文件里（不影响使用）：
集合名材质（标记 `mbakery_final_material`）、物体自定义属性 `mbakery_*`、
输出目录里的 `_mbakery.json`。

## 手动安装（不想用 zip）

把 `MaterialBakery\material_bakery\` 整个目录复制到：

| 系统 | 路径 |
|---|---|
| Windows | `%APPDATA%\Blender Foundation\Blender\<版本>\scripts\addons\` |
| macOS | `~/Library/Application Support/Blender/<版本>/scripts/addons/` |
| Linux | `~/.config/blender/<版本>/scripts/addons/` |

然后重启 Blender、在 Add-ons 里启用。目录名必须是 `material_bakery`（下划线，不能有空格/点）。

## 打包时注意（自己重新出 zip）

用 `tools\build_zips.ps1`，**不要用 `Compress-Archive`** —— 它写出来的条目名带反斜杠
（`material_bakery\ui\ops.py`），Blender 会把整条当成一个超长文件名，装出来的目录是坏的。

打包前先跑 `tools\sync_package.ps1`：zip 的来源是 `MaterialBakery\material_bakery\`，
**不是**工作区里那份 `material_bakery\`。忘了同步的话，装上去的还是上一版代码
（这个坑真踩过：v1.0.3 打出来的包里 `__init__.py` 还写着 1.0.2）。

完整流程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_package.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_zips.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1
```

"""验证**打好的 zip**能不能真的被 Blender 装上。

跟 `mb_test_install.py` 的区别：那边验的是"文件夹复制过去"，
这边走的是用户真正会用的路径 —— `Install...` 选一个 zip。
两件事都值得单独验，因为它们的失败方式完全不同：

  * 文件夹复制：漏文件、多 __pycache__、目录名不合法
  * 装 zip：**条目名的分隔符**。用 Compress-Archive 打出来的包，
    条目名是 `material_bakery\\ui\\ops.py`，Blender 会把它当成一个
    超长的文件名 —— Windows 上恰好还能拆成目录（反斜杠也是分隔符），
    Linux/macOS 上就直接装出一个叫 `material_bakery\\ui\\ops.py` 的文件。

⚠ 必须在**启动前**把隔离配置目录准备好（addon 安装目标就是它）：

```powershell
$ws = "D:\\Codex Projects\\Blender Addons\\Auto Bake v1.5"
$probe = "$ws\\_probe\\zipinstall"
if (Test-Path $probe) { Remove-Item -Recurse -Force $probe }
New-Item -ItemType Directory -Force -Path "$probe\\scr\\addons" | Out-Null
$env:BLENDER_USER_CONFIG  = "$probe\\cfg"
$env:BLENDER_USER_SCRIPTS = "$probe\\scr"
& "C:\\Program Files\\Blender Foundation\\Blender 4.5\\blender.exe" `
    --background --factory-startup --python "$ws\\tests\\mb_test_zip_install.py"
```
"""
import bpy, sys, os, glob, zipfile

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(WS, "MaterialBakery", "material_bakery_install.zip")

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

print("=== 隔离环境自检（别污染真实配置）===")
scripts = bpy.utils.script_path_user()
print("  脚本目录:", scripts)
in_probe = os.path.normcase(scripts).startswith(os.path.normcase(WS))
check("脚本目录在工作区的 _probe 里（不会写进真实 Blender 设置）", in_probe, scripts)
if not in_probe:
    print("  没设 BLENDER_USER_SCRIPTS，直接退出 —— 见本文件头部说明")
    sys.exit(1)

print("\n=== zip 本身 ===")
check("zip 存在", os.path.isfile(ZIP), ZIP)
if not os.path.isfile(ZIP):
    # ⚠ 以前这里直接 zipfile.ZipFile() -> FileNotFoundError -> 整个套件在 0.5 秒内
    #   崩掉，只剩 1 项检查，而且看不出到底哪一步没做。zip 是**构建产物**，
    #   它不在的时候要说清楚怎么造它，然后**干净地**报失败。
    print("  没有 zip，先造一个：")
    print("      powershell -NoProfile -ExecutionPolicy Bypass -File tools\\build_zips.ps1")
    print("  （run_all_tests.ps1 会在跑本套件前自动补造，手工跑时要自己来）")
    print("\n=== 结果 ===")
    print("FAILED 1 check(s):")
    print("  - zip 存在")
    sys.exit(1)
with zipfile.ZipFile(ZIP) as archive:
    names = archive.namelist()
    bad = [n for n in names if "\\" in n]
    roots = sorted({n.split("/")[0] for n in names})
    print("  条目数:", len(names), " 根目录:", roots)
    check("条目名全部用正斜杠（Compress-Archive 会写反斜杠）", not bad, bad[:3])
    check("根目录是 material_bakery/", roots == ["material_bakery"], roots)
    check("有 __init__.py", "material_bakery/__init__.py" in names)
    check("没有 __pycache__ / .pyc",
          not any("__pycache__" in n or n.endswith(".pyc") for n in names),
          [n for n in names if "__pycache__" in n][:3])
    check("四个子包都在",
          all("material_bakery/{}/".format(p) in " ".join(names)
              for p in ("core", "engine", "deliver", "ui")))

print("\n=== 用 addon_install 真的装一遍 ===")
result = bpy.ops.preferences.addon_install(filepath=ZIP, overwrite=True)
check("addon_install 返回 FINISHED", result == {'FINISHED'}, result)

installed = os.path.join(scripts, "addons", "material_bakery")
check("装出来的目录存在", os.path.isdir(installed), installed)

try:
    enabled = bpy.ops.preferences.addon_enable(module="material_bakery")
except Exception as exc:
    enabled = "{}: {}".format(type(exc).__name__, exc)
check("装上就能启用", enabled == {'FINISHED'}, enabled)

import material_bakery as loaded
check("加载的是刚装的那份",
      os.path.normcase(os.path.dirname(loaded.__file__))
      == os.path.normcase(installed),
      (os.path.dirname(loaded.__file__), installed))
# ⚠ 版本号不要写死（每发一版都会 +1，写死了就是假失败）
_version = getattr(loaded, "bl_info", {}).get("version", (0, 0, 0))
check("版本号是合法的三段元组", isinstance(_version, tuple) and len(_version) == 3,
      _version)
check("包里有打包时间戳", bool(getattr(loaded, "__build__", "")),
      getattr(loaded, "__build__", ""))
check("Scene 上的版本标记跟 bl_info 一致",
      getattr(bpy.context.scene, "mbakery_version", "")
      == ".".join(str(v) for v in _version),
      getattr(bpy.context.scene, "mbakery_version", None))
check("向导属性挂上了", hasattr(bpy.context.scene, "mbakery"))
check("N 面板注册了", getattr(bpy.types, "MBAKERY_PT_wizard", None) is not None)

# 目录结构真的对（不是被拆成一堆怪名字的文件）
missing = []
for part in ("core", "engine", "deliver", "ui"):
    if not os.path.isdir(os.path.join(installed, part)):
        missing.append(part)
check("装完四个子包都是目录", not missing, missing)
py_files = glob.glob(os.path.join(installed, "**", "*.py"), recursive=True)
# ⚠ 不要写死数量。原来这里写的是 34，加了两个模块（core/imported.py、
#   ui/ops_rescue.py）之后就成了 36 —— 过期断言看起来像"装坏了"。
#   改成跟 zip 里的 .py 条目数对齐，以后加模块不用再改这个测试。
with zipfile.ZipFile(ZIP) as archive:
    declared_py = [n for n in archive.namelist() if n.endswith(".py")]
check("装出的 .py 数量跟 zip 里一致（{} 个）".format(len(declared_py)),
      len(py_files) == len(declared_py), (len(py_files), len(declared_py)))
weird = [os.path.basename(p) for p in py_files if "\\" in os.path.basename(p)]
check("没有名字里带反斜杠的怪文件", not weird, weird[:3])

print("\n=== 卸载 ===")
bpy.ops.preferences.addon_disable(module="material_bakery")
check("能正常禁用",
      "material_bakery" not in bpy.context.preferences.addons.keys())

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

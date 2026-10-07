"""Material Bakery —— 导出层测试：清单（救援 / 重开文件）+ 空状态 + 回归

这一套是实施轮 1.5 那套的**删减版**（1.6 把"导出时合并 alpha"整条路删了：
base_color 现在在**烘完写盘之前**就带 alpha，见 `tests/mb_test_base_alpha.py`）。

覆盖：
  A. 清单：`build_manifest` 的三来源（真烘过用报告 / 扫回来的贴图 / 重开文件后还在的
     持久列表）、失败条目与空行不进清单；导出输入是清单、**不是** report
  B. 救援状态（没有 job、**没有 report**）：两个导出入口都能出图、不抛异常
     —— 1.5 修的那个 `AttributeError: 'NoneType' object has no attribute 'results'`
  C. 空状态：给的是那句明确错误，不是 `AttributeError`
  D. 清单与逐张勾选**没有交集**时必须点名报错（1.6 顺手修的"导 0 张且不报错"）
  E. 回归：`Export Textures` 的产物与"Blender 自己 save() 的副本"逐字节一致，
     文件名叫 `image_name`（不带任何后缀）
  F. alpha 相关的旧机制真的没了（勾选框 / 按钮 / 后缀 / 合并函数）

判据全部是打印出来的数字（MD5、目录清单、位深）。不硬编码版本号 / 绝对路径 / 计数。
"""
import hashlib
import inspect
import os
import shutil
import sys

import bpy

# 工作区 = 本文件所在目录的上一级。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import presets as presets_mod
from material_bakery.deliver import textures as tex
from material_bakery.engine import report as report_mod
from material_bakery.ui import panels as upanels
from material_bakery.ui import properties as uprops
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []


def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


OUT = os.path.join(WS, "_probe", "export")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)

BASE_NAME = "Body_base_color"
ALPHA_NAME = "Body_shader_alpha"


def make_image(name, size, rgb=(0.25, 0.5, 0.75)):
    image = bpy.data.images.new(name, width=size[0], height=size[1], alpha=False,
                                float_buffer=False)
    image.colorspace_settings.name = "sRGB"
    count = size[0] * size[1]
    buffer = []
    for _ in range(count):
        buffer.extend([rgb[0], rgb[1], rgb[2], 1.0])
    image.pixels.foreach_set(buffer)
    image.file_format = 'PNG'
    return image


def make_report(entries):
    report = report_mod.RunReport(label="export")
    for group, type_key, image_name in entries:
        image = bpy.data.images.get(image_name)
        size_x, size_y = (image.size[0], image.size[1]) if image else (0, 0)
        report.add(report_mod.TaskResult(
            group=group, type_key=type_key, type_label=type_key,
            size_x=size_x, size_y=size_y, image_name=image_name,
            status=report_mod.STATUS_DONE))
    return report


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.md5(handle.read()).hexdigest()


def depth_of(path):
    image = bpy.data.images.load(path)
    depth = image.depth
    bpy.data.images.remove(image)
    return depth


def call(operator, **kwargs):
    """调用 operator，返回 `(result, message)`

    ⚠ message 必须看：`bpy.ops` 把"内部真炸了"（AttributeError 之类）也包成
      RuntimeError，只看 `{'CANCELLED'}` 分不出"干净报错"和"代码炸了"。
    """
    try:
        return operator(**kwargs), ""
    except RuntimeError as exc:
        print("      (operator 报错被拦: {})".format(exc))
        return {'CANCELLED'}, str(exc)


def set_textures(entries):
    settings = bpy.context.scene.mbakery
    settings.textures.clear()
    for group, type_key, image_name in entries:
        item = settings.textures.add()
        item.group = group
        item.type_key = type_key
        item.type_label = type_key
        item.image_name = image_name
        item.enabled = True


# --------------------------------------------------------------------------- A
print("=== 阶段 A：清单与设施 ===")
material_bakery.register()
settings = bpy.context.scene.mbakery

check("ExportItem 是纯数据（不碰 bpy 对象）",
      tex.ExportItem("x", "Group", "shader.base_color").group == "Group")
check("export_report 的第一个参数是清单、不再是 report",
      list(inspect.signature(tex.export_report).parameters)[0] == "manifest",
      list(inspect.signature(tex.export_report).parameters))
check("1.4/1.5 的勾选框字段没有留下", not hasattr(settings, "merge_alpha"))
check("预设字段表里也没有它", "merge_alpha" not in presets_mod.FIELDS)
check("export_report 不再有 merge_alpha 参数",
      "merge_alpha" not in inspect.signature(tex.export_report).parameters,
      list(inspect.signature(tex.export_report).parameters))

set_textures([])
check("没有报告、贴图列表也空 → 清单为空",
      tex.build_manifest(None, settings) == [], tex.build_manifest(None, settings))
set_textures([("ListGroup", "shader.base_color", "FromList")])
listed = tex.build_manifest(None, settings)
check("没有报告时清单从贴图列表来（救援 / 重开文件）",
      [entry.image_name for entry in listed] == ["FromList"], listed)
check("清单项带着组名与类型",
      listed[0].group == "ListGroup" and listed[0].type_key == "shader.base_color",
      listed[0])
report_for_manifest = make_report([("R", "shader.roughness", "FromReport")])
from_report = tex.build_manifest(report_for_manifest, settings)
check("有报告时以报告为准（它才是『这一轮烘了什么』的权威记录）",
      [entry.image_name for entry in from_report] == ["FromReport"], from_report)
a_report = report_mod.RunReport()
a_report.add(report_mod.TaskResult(group="G", type_key="shader.alpha",
                                   image_name="failed_one",
                                   status=report_mod.STATUS_FAILED))
check("失败的条目不进清单", tex.manifest_from_report(a_report) == [],
      tex.manifest_from_report(a_report))
set_textures([])
row = settings.textures.add()
row.image_name = ""
check("没有图名的空行不进清单", tex.build_manifest(None, settings) == [],
      tex.build_manifest(None, settings))
settings.textures.clear()

# --------------------------------------------------------------------------- B
print("\n=== 阶段 B：救援状态导出（没有 report）===")
base = make_image(BASE_NAME, (32, 32))
alpha = make_image(ALPHA_NAME, (32, 32))
active = usession.Session.get()
rescue_dir = os.path.join(OUT, "rescue")
os.makedirs(rescue_dir, exist_ok=True)
settings.output_dir = rescue_dir
settings.use_subfolders = False
settings.file_format = 'PNG'
set_textures([("Body", "shader.base_color", BASE_NAME),
              ("Body", "shader.alpha", ALPHA_NAME)])
active.job = None
active.report = None
active.imported_by_group = {"Body": [base, alpha]}
check("前提成立：救援状态里 report 真的是 None（别让夹具比现实更完整）",
      active.report is None and active.job is None
      and bool(active.imported_by_group), active.imported_by_group)

result, message = call(bpy.ops.mbakery.export_textures)
check("救援状态：Export Textures 出图、不抛异常", result == {'FINISHED'}, (result, message))
check("救援状态：两张都写出来了（名字就是原名）",
      all(os.path.isfile(tex.target_path(name, 'PNG', rescue_dir))
          for name in (BASE_NAME, ALPHA_NAME)), sorted(os.listdir(rescue_dir)))
result, message = call(bpy.ops.mbakery.export_one_texture, index=0)
check("救援状态：逐张 Export 出图、不抛异常", result == {'FINISHED'}, (result, message))
check("消息里没有 'NoneType'/'AttributeError'（1.5 修的就是这个）",
      "NoneType" not in message and "AttributeError" not in message, message)

# --------------------------------------------------------------------------- C
print("\n=== 阶段 C：空状态给明确错误（不是 AttributeError）===")
empty_dir = os.path.join(OUT, "empty")
os.makedirs(empty_dir, exist_ok=True)
settings.output_dir = empty_dir
active.report = None
active.imported_by_group = {}
settings.textures.clear()
for name, kwargs in (("Export Textures", {}), ("逐张 Export", {"index": 0})):
    operator = {"Export Textures": bpy.ops.mbakery.export_textures,
                "逐张 Export": bpy.ops.mbakery.export_one_texture}[name]
    result, message = call(operator, **kwargs)
    check("{}：拦下来了（CANCELLED）".format(name), result == {'CANCELLED'}, result)
    check("{}：给的是那句明确的错误，不是 'NoneType'".format(name),
          "Nothing to export" in message and "NoneType" not in message, message)
check("空状态没有产出任何文件", not os.listdir(empty_dir), os.listdir(empty_dir))

# --------------------------------------------------------------------------- D
print("\n=== 阶段 D：清单与勾选对不上 → 点名报错（1.6）===")
mismatch_dir = os.path.join(OUT, "mismatch")
os.makedirs(mismatch_dir, exist_ok=True)
settings.output_dir = mismatch_dir
active.report = make_report([("Body", "shader.base_color", BASE_NAME)])
# 贴图列表里全是**别的**名字（模拟"烘完又扫了目录"）：清单 1 条，勾选 0 条命中
set_textures([("Other", "shader.roughness", "SomeOther_roughness")])
result, message = call(bpy.ops.mbakery.export_textures)
check("对不上时被拦下（不再静默导 0 张）", result == {'CANCELLED'}, (result, message))
check("报错点名了、并要求刷新列表",
      "Nothing selected to export" in message and "Refresh List" in message, message)
check("目录里没有产出任何文件", not os.listdir(mismatch_dir), os.listdir(mismatch_dir))
messages = [entry.message for entry in active.log]
check("这条也进了会话日志（有迹可循）",
      any("Nothing selected to export" in text for text in messages),
      [t for t in messages if "export" in t.lower()][:3])

set_textures([("Body", "shader.base_color", BASE_NAME)])
result, message = call(bpy.ops.mbakery.export_textures)
check("刷新列表之后导出成功", result == {'FINISHED'}, (result, message))
check("导出的文件名就是 image_name",
      sorted(os.listdir(mismatch_dir)) == [BASE_NAME + ".png"],
      sorted(os.listdir(mismatch_dir)))

# --------------------------------------------------------------------------- E
print("\n=== 阶段 E：回归（与 Blender 自己 save() 逐字节一致）===")
regress_dir = os.path.join(OUT, "regress")
reference_dir = os.path.join(OUT, "reference")
for folder in (regress_dir, reference_dir):
    os.makedirs(folder, exist_ok=True)
settings.output_dir = regress_dir
active.report = make_report([("Body", "shader.base_color", BASE_NAME),
                             ("Body", "shader.alpha", ALPHA_NAME)])
set_textures([("Body", "shader.base_color", BASE_NAME),
              ("Body", "shader.alpha", ALPHA_NAME)])
result, message = call(bpy.ops.mbakery.export_textures)
check("Export Textures 跑通", result == {'FINISHED'}, (result, message))
check("每张各导各的（两张，文件名都是原名）",
      sorted(os.listdir(regress_dir)) == sorted([BASE_NAME + ".png", ALPHA_NAME + ".png"]),
      sorted(os.listdir(regress_dir)))
reference_path = os.path.join(reference_dir, BASE_NAME + ".png")
base.filepath_raw = reference_path
base.file_format = 'PNG'
base.save()
exported_path = tex.target_path(BASE_NAME, 'PNG', regress_dir)
print("  导出 vs Blender 自己 save():", digest(exported_path)[:12],
      digest(reference_path)[:12])
check("与『Blender 自己 save() 的副本』逐字节一致（导出这一步不加工）",
      digest(exported_path) == digest(reference_path))
check("导出的还是 24 位（这张图本来就没有 alpha 通道）",
      depth_of(exported_path) == 24, depth_of(exported_path))

# --------------------------------------------------------------------------- F
print("\n=== 阶段 F：alpha 旧机制真的没了 ===")
check("Result 页那个按钮的 operator 已经删除",
      "export_base_color_with_alpha" not in dir(bpy.ops.mbakery),
      [n for n in dir(bpy.ops.mbakery) if "export" in n])
check("deliver.textures 里没有任何 alpha/合并函数",
      not any(hasattr(tex, name) for name in
              ("merge_alpha_plan", "export_alpha_base_colors", "alpha_product_name",
               "alpha_image_for", "alpha_lost", "export_merged")),
      [n for n in dir(tex) if "alpha" in n.lower() or "merge" in n.lower()])
check("面板上没有合并勾选框的绘制函数",
      not hasattr(upanels, "draw_alpha_merge_row")
      and not hasattr(upanels, "merge_alpha_ready"))
check("设置里没有『带/不带 alpha』的开关",
      not any("alpha" in p.identifier.lower()
              for p in settings.bl_rna.properties
              if p.identifier not in ("bl_rna", "rna_type")),
      [p.identifier for p in settings.bl_rna.properties
       if "alpha" in p.identifier.lower()])
check("清单化修复保留着（build_manifest / exported_names）",
      hasattr(tex, "build_manifest") and hasattr(tex, "exported_names")
      and hasattr(uprops, "PAGE_RESULT"))

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

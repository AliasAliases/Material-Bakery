"""把面板里用到的每个 icon 都查一遍 —— 包括**位置参数**传的

为什么重写：第一版只扫 `icon='...'` 这种关键字写法，
而 `_box(layout, "UV", 'UV_ISLANDS')` 是把 icon 当**第三个位置参数**传进
自己的辅助函数的 —— 于是 `UV_ISLANDS` 这个不存在的名字一路漏到用户界面上，
把整页的绘制打挂（表现就是"某些控件不见了"）。

现在两种写法都扫：
    - `icon='X'` / `icon_value=` 关键字
    - `_box(layout, title, 'X')` 这类辅助函数的位置参数
"""
import ast
import os
import sys

import bpy

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(WS, "material_bakery", "ui")

# icon 作为第 3 个位置参数的辅助函数（0-based: 下标 2）
HELPERS_WITH_ICON_ARG = {"_box": 2}


def valid_icons():
    names = set()
    for function_name in ("label", "operator", "prop", "prop_enum", "prop_search",
                          "separator", "menu", "menu_pie"):
        try:
            parameters = bpy.types.UILayout.bl_rna.functions[function_name].parameters
        except (KeyError, AttributeError):
            continue
        prop = parameters.get("icon")
        if prop is None:
            continue
        names.update(item.identifier for item in prop.enum_items)
    return names


VALID = valid_icons()
print("Blender", bpy.app.version_string, "| 合法 icon:", len(VALID))

findings = []          # (文件, 行号, icon 名, 写法, 是否合法)
used = set()

for filename in sorted(os.listdir(UI_DIR)):
    if not filename.endswith(".py"):
        continue
    path = os.path.join(UI_DIR, filename)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # 1) icon= / icon_value= 关键字
        for keyword in node.keywords:
            if keyword.arg != "icon":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                used.add(value.value)
                findings.append((filename, node.lineno, value.value,
                                 "icon=", value.value in VALID))

        # 2) 辅助函数的位置参数
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        index = HELPERS_WITH_ICON_ARG.get(name)
        if index is None or len(node.args) <= index:
            continue
        value = node.args[index]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            used.add(value.value)
            findings.append((filename, node.lineno, value.value,
                             "{} positional".format(name), value.value in VALID))

print("扫到 icon 引用:", len(findings), "| 不同的名字:", len(used))

bad = [f for f in findings if not f[4]]
if bad:
    print("\n❌ 不存在的 icon 名：")
    for filename, lineno, icon, style, _ok in sorted(bad, key=lambda f: (f[0], f[1])):
        print("   {:<12} line {:<4} {!r:<18} ({})".format(filename, lineno, icon, style))
    print("\n可用的近似名字（供挑选）：")
    for filename, lineno, icon, style, _ok in sorted(bad, key=lambda f: (f[0], f[1])):
        root = icon.split("_")[0]
        near = sorted(n for n in VALID if root in n)
        print("   {!r} -> {}".format(icon, ", ".join(near[:12]) or "(没有相近的)"))
else:
    print("\n✅ 所有 icon 名都合法（关键字 + 位置参数两种写法都查了）")

sys.exit(1 if bad else 0)

"""Material Bakery — 阶段 0/1 核心测试
覆盖：类型注册表、命名策略、材质仓库、材质槽交付
"""
import bpy, sys, os

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import naming as nm
from material_bakery.core import materials as mt

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

print("=== 阶段 0：骨架与注册 ===")
material_bakery.register()
check("插件注册成功", hasattr(bpy.context.scene, "mbakery_version"))
# ⚠ 不要写死版本号：每次发新版都会 +1，写死了就是一条假失败
#   （同类错误在这个项目里出现过三次：operator 数量、.py 文件数、现在是版本号）。
_expected_version = ".".join(str(v) for v in
                             material_bakery.bl_info.get("version", (0, 0, 0)))
check("版本标记跟 bl_info 一致",
      bpy.context.scene.mbakery_version == _expected_version,
      (bpy.context.scene.mbakery_version, _expected_version))
check("面板上能显示出版本（用户要知道自己装的是哪一版）",
      bool(getattr(material_bakery, "__build__", "")),
      getattr(material_bakery, "__build__", ""))
print("  Blender:", compat.VERSION_STRING, " IS_4X =", compat.IS_4X)
check("compat 能判断版本", isinstance(compat.IS_4X, bool))

supported, dropped = compat.supported_kwargs(bpy.ops.object.bake, {
    "type": "EMIT",
    "use_clear": True,
    "this_param_does_not_exist": 1,
})
print("  supported_kwargs -> 可用 {} 个，丢弃 {}".format(len(supported), dropped))
check("过滤掉不存在的参数", dropped == ["this_param_does_not_exist"], dropped)
check("保留存在的参数", "type" in supported and "use_clear" in supported)

# ------------------------------------------------------------------ 类型注册表
print("\n=== 阶段 1a：烘焙类型注册表 ===")
print("  注册表条目数:", len(bt.REGISTRY))
# 44 = 原版的 46 减去 multires 的两个类型（已明确删除，原因见 bake_types.py）
check("类型数量为 44", len(bt.REGISTRY) == 44, len(bt.REGISTRY))
check("全部 key 唯一", len(set(bt.REGISTRY)) == len(bt.REGISTRY))
check("ORDER 覆盖全部", len(bt.ORDER) == len(bt.REGISTRY))

# 原版的设计地雷：靠尾随空格区分
check("没有靠尾随空格区分的 key",
      all(k == k.strip() and " " not in k for k in bt.REGISTRY))
keys_with_space = [k for k in bt.REGISTRY if " " in k]
check("key 里没有空格", not keys_with_space, keys_with_space)

# multires 是**故意**删掉的，不是漏了
check("没有 multires 类型",
      not [k for k in bt.REGISTRY if k.startswith("multires.")],
      [k for k in bt.REGISTRY if k.startswith("multires.")])
check("BakeKind 里也没有 MULTIRES",
      not hasattr(bt.BakeKind, "MULTIRES"))
check("分组表里没有 Multires", "Multires" not in bt.GROUP_ORDER, bt.GROUP_ORDER)
check("原版的 multires 旧名不再映射到任何类型（会被迁移丢掉并记账）",
      bt.from_original_label("Normals") is None
      and bt.from_original_label("Displacement") is None,
      (bt.from_original_label("Normals"), bt.from_original_label("Displacement")))

for key in ("misc.displacement", "shader.normal", "standard.normal",
            "shader.roughness", "standard.roughness"):
    check("存在 {}".format(key), bt.get(key) is not None)

check("misc.displacement 走 EMIT",
      bt.require("misc.displacement").bake_pass == "EMIT")
check("两种 Normal 是不同的 bake pass",
      bt.require("shader.normal").bake_pass == "NORMAL"
      and bt.require("standard.normal").bake_pass == "NORMAL")

print("\n  分组:")
groups = bt.by_group()
for group, entries in groups.items():
    print("    {:<16} {} 种".format(group, len(entries)))
check("分组数量（原版 7 个去掉 Multires）", len(groups) == 6, list(groups))
check("分组条目总数等于注册表", sum(len(v) for v in groups.values()) == len(bt.REGISTRY))

print("\n  通道分布:")
from collections import Counter
channel_counts = Counter(t.channel.value for t in bt.iterator())
for channel, count in sorted(channel_counts.items()):
    print("    {:<8} {}".format(channel, count))
check("三种通道都有", len(channel_counts) == 3, dict(channel_counts))

print("\n  socket 别名（版本无关的关键）:")
samples = [
    ("shader.coat_weight", ("Coat Weight", "Clear Coat", "Clearcoat")),
    ("shader.subsurface_weight", ("Subsurface Weight", "Subsurface")),
    ("shader.emission_color", ("Emission Color", "Emissive Color", "Emission")),
    ("shader.base_color", ("Base Color", "Color", "Tint")),
]
for key, expected in samples:
    actual = bt.require(key).sockets
    print("    {:<28} {}".format(key, actual))
    check("{} 别名齐全（3.x+4.x）".format(key), actual == expected, actual)

print("\n  每类型参数:")
for key, expected in (("misc.channel_packing", ("channel_r", "channel_g", "channel_b")),
                      ("misc.ao", ("ao_sample", "ao_sample_use", "ao_inside",
                                   "ao_only_local", "ao_distance")),
                      ("misc.pointiness", ("pointiness_contrast", "pointiness_brightness"))):
    actual = bt.require(key).params
    check("{} 的参数正确".format(key), actual == expected, actual)

check("NORMAL pass 自动带 normal_space",
      "normal_space" in bt.require("shader.normal").all_params
      and "normal_space" not in bt.require("shader.metallic").all_params)
check("派生参数不重复",
      len(bt.require("shader.normal").all_params) == len(set(bt.require("shader.normal").all_params)))

print("\n  枚举项生成:")
items = bt.enum_items()
headers = [i for i in items if i[0] == '']
print("    总条目 {}，其中分组标题 {}: {}".format(
    len(items), len(headers), [h[1] for h in headers]))
check("枚举条目 = 类型 + 分组标题", len(items) == len(bt.REGISTRY) + len(headers))
check("每个分组只有一个标题", len(headers) == len(bt.by_group()), len(headers))
check("没有空 label 的实体项", all(i[1] for i in items if i[0] != ''))

print("\n  混合体检查（标准 pass + 节点手术）:")
uv_type = bt.require("standard.uv")
print("    standard.uv: kind={} bake_pass={} params={}".format(
    uv_type.kind.value, uv_type.bake_pass, uv_type.params))
check("UV 走 UV_MAP 节点手术", uv_type.kind is bt.BakeKind.UV_MAP)
check("UV 的 bake_pass 仍是 UV", uv_type.bake_pass == "UV")
check("UV 暴露 uv_map 参数", uv_type.params == ("uv_map",))
check("没有重复的 UV 类型",
      len([k for k in bt.REGISTRY if bt.REGISTRY[k].label == "UV"]) == 1)

print("\n  旧名迁移（预设与对照测试要用）:")
legacy_cases = [
    ("Displacement ", "misc.displacement"),      # 尾随空格
    ("Ambient Occlusion ", "standard.ao"),       # 尾随空格
    ("Ambient Occlusion", "misc.ao"),
    ("Normal ", "standard.normal"),
    ("Normal", "shader.normal"),
    ("Roughness ", "standard.roughness"),
    ("Roughness", "shader.roughness"),
    ("Clearcoat", "shader.coat_weight"),         # 3.x 旧名
    ("Sheen", "shader.sheen_weight"),
    ("Specular", "shader.specular_ior_level"),
    ("Emission", "shader.emission_color"),
]
for label, expected in legacy_cases:
    actual = bt.from_original_label(label)
    check("迁移 {!r} -> {}".format(label, expected), actual == expected, actual)
check("不认识的返回 None", bt.from_original_label("NoSuchType") is None)
check("新 key 直接通过", bt.from_original_label("shader.metallic") == "shader.metallic")
check("全部 46 个新 key 都能反查到原版名",
      all(k in bt.KEY_TO_ORIGINAL_LABEL for k in bt.REGISTRY),
      [k for k in bt.REGISTRY if k not in bt.KEY_TO_ORIGINAL_LABEL])

# ------------------------------------------------------------------ 命名
print("\n=== 阶段 1b：命名策略 ===")
check("清洗保留字符", nm.sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij",
      nm.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'))
check("类型名去空格", nm.type_token("Base Color") == "BaseColor", nm.type_token("Base Color"))
check("类型名去尾随空格", nm.type_token("Normal ") == "Normal", nm.type_token("Normal "))

ok, err = nm.validate_template("{prefix}{type}{bridge}{size}{suffix}")
check("默认模板通过", ok, err)
ok, err = nm.validate_template("{prefix}_{type}_{size}")
check("下划线模板通过", ok, err)
check("未知 key 报错", not nm.validate_template("{nope}")[0])
check("括号不配对报错", not nm.validate_template("{prefix")[0])
check("无变量报错", not nm.validate_template("static")[0])
check("普通模板不许含 udim", not nm.validate_template("{prefix}{udim}")[0])
check("UDIM 导出模板必须含 udim",
      not nm.validate_template("{prefix}{type}", allow_udim=True)[0]
      and nm.validate_template("{prefix}{udim}", allow_udim=True)[0])

vars_ = {"prefix": "Body", "type": "BaseColor", "bridge": "_", "size": "2048", "suffix": ""}
check("渲染名字", nm.render_name("{prefix}_{type}_{size}", vars_) == "Body_BaseColor_2048",
      nm.render_name("{prefix}_{type}_{size}", vars_))
check("缺变量不崩", nm.render_name("{prefix}_{type}_{size}", {"prefix": "Body"}) == "Body__",
      repr(nm.render_name("{prefix}_{type}_{size}", {"prefix": "Body"})))
check("非法模板退化成默认结构不崩",
      isinstance(nm.render_name("{bad", vars_), str))
check("渲染后清洗保留字符",
      "<" not in nm.render_name("{prefix}", {"prefix": "a<b"}))

print("\n  前缀决策（唯一一处）:")
prefix_cases = [
    # (prefix设置, 集合名, 物体名, auto_mode, 期望)
    ("", "Body", "Body_Cube", False, "Body"),          # 有集合用集合名
    ("", "", "Sphere", False, "Sphere"),               # 无集合用物体名
    ("MyPrefix", "Body", "Body_Cube", False, "MyPrefix"),
    ("", "Body", "Body_Cube", True, "Body"),           # auto 模式仍走集合名
    ("MyPrefix", "Body", "Body_Cube", True, "Body"),   # auto 模式忽略 Prefix
    ('""', "Body", "Body_Cube", False, ""),            # 明确不要前缀
    ("''", "", "Sphere", False, ""),
    ("MyPrefix", "", "Sphere", True, "Sphere"),
]
for prefix, coll, obj, auto, expected in prefix_cases:
    actual = nm.select_prefix(prefix, coll, obj, auto)
    check("select_prefix({!r}, {!r}, auto={}) -> {!r}".format(prefix, coll, auto, expected),
          actual == expected, actual)

print("\n  撞名消解:")
entries = [("Body_Cube", "Body"), ("Body_Sphere", "Body"), ("Head", "Head")]
resolved = nm.resolve_collisions(entries)
print("   ", resolved)
check("同集合两物体不再撞名", resolved["Body_Cube"] != resolved["Body_Sphere"], resolved)
check("不同集合保持干净名字", resolved["Head"] == "Head", resolved)
check("撞名时保留集合名",
      resolved["Body_Cube"].startswith("Body") and resolved["Body_Sphere"].startswith("Body"))

taken = set()
check("unique_name 首个通过", nm.unique_name("Body", taken) == "Body")
check("unique_name 撞名加序号", nm.unique_name("Body", taken) == "Body.001")
check("unique_name 继续递增", nm.unique_name("Body", taken) == "Body.002")

lines, total = nm.preview("{prefix}_{type}", [{"prefix": "A", "type": "T"}] * 10, limit=3)
check("预览限制行数", len(lines) == 3 and total == 10, (len(lines), total))

# ------------------------------------------------------------------ 材质
print("\n=== 阶段 1c：材质仓库 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
store = mt.MaterialStore()

first = store.acquire("Body", mt.MARKER_FINAL)
first_ptr = first.as_pointer()
print("  第 1 次:", first.name, "ptr=", first_ptr)
check("第 1 次建出正确名字", first.name == "Body", first.name)
check("带标记", bool(first.get(mt.MARKER_FINAL)))
check("节点树为 Output + Principled",
      sorted(n.type for n in first.node_tree.nodes) == ["BSDF_PRINCIPLED", "OUTPUT_MATERIAL"])
check("Output 有输入连接", first.node_tree.nodes["Material Output"].inputs[0].is_linked)

for i in range(4):
    again = store.acquire("Body", mt.MARKER_FINAL)
    check("第 {} 次复用同一数据块".format(i + 2), again.as_pointer() == first_ptr)
check("重取 5 次后材质总数仍为 1", len(bpy.data.materials) == 1, len(bpy.data.materials))

# 覆盖旧节点
first.node_tree.nodes.new("ShaderNodeTexImage")
first.node_tree.nodes.new("ShaderNodeTexImage")
check("塞入节点后是 4 个", len(first.node_tree.nodes) == 4)
refreshed = store.acquire("Body", mt.MARKER_FINAL)
check("覆盖时清空旧节点",
      sorted(n.type for n in refreshed.node_tree.nodes) == ["BSDF_PRINCIPLED", "OUTPUT_MATERIAL"])

# 用户的同名材质
user_mat = bpy.data.materials.new(name="Sphere")
user_mat.use_nodes = True
user_ptr = user_mat.as_pointer()
ours = store.acquire("Sphere", mt.MARKER_FINAL)
names = sorted(m.name for m in bpy.data.materials)
print("  用户材质存在时:", names)
check("插件材质拿到名字", ours.name == "Sphere" and ours.as_pointer() != user_ptr)
check("用户材质被改名保留", any(m.as_pointer() == user_ptr for m in bpy.data.materials), names)
check("用户材质没被标记", not user_mat.get(mt.MARKER_FINAL))
check("owns() 只认自己的", store.owns(ours) and not store.owns(user_mat))

for i in range(3):
    store.acquire("Sphere", mt.MARKER_FINAL)
print("  材质总数:", sorted(m.name for m in bpy.data.materials))
check("与用户材质重名时不再新增副本", len(bpy.data.materials) == 3, len(bpy.data.materials))
check("用户材质仍以 .001 保留", any(m.as_pointer() == user_ptr for m in bpy.data.materials))

# simple 模式（给无材质物体补材质用）
simple = store.acquire("Helper", mt.MARKER_HELPER, simple=True)
check("simple 模式清空节点", len(simple.node_tree.nodes) == 0, len(simple.node_tree.nodes))
check("simple 模式带自己的标记", bool(simple.get(mt.MARKER_HELPER)))
check("两个标记键不同", mt.MARKER_FINAL != mt.MARKER_HELPER)
summary = store.summary()
print("  仓库统计: created={} reused={} displaced={}".format(
    len(summary["created"]), len(summary["reused"]), len(summary["displaced"])))
check("统计有记录", summary["created"] and summary["reused"])

# ------------------------------------------------------------------ 材质槽交付
print("\n=== 阶段 1d：材质槽交付（应用 / 恢复）===")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
obj = bpy.context.active_object
obj.name = "Part"

m_orig_a = bpy.data.materials.new("OriginalA")
m_orig_b = bpy.data.materials.new("OriginalB")
m_baked = bpy.data.materials.new("Body")
obj.data.materials.append(m_orig_a)
obj.data.materials.append(m_orig_b)
obj.data.materials.append(m_baked)
# 前面的面用 OriginalA(0)，后面的面用 OriginalB(1) —— 真正的多材质物体
for i, poly in enumerate(obj.data.polygons):
    poly.material_index = 0 if i < 3 else 1
print("  初始槽:", [s.material.name for s in obj.material_slots])
print("  初始逐面索引:", [p.material_index for p in obj.data.polygons])
print("  初始实际显示:", mt.visible_material_names(obj))
check("初始显示的是原材质",
      set(mt.visible_material_names(obj)) == {"OriginalA", "OriginalB"},
      mt.visible_material_names(obj))

# ---- 反面教材：原版/旧版的做法（追加槽 + 改 active）为什么无效
obj.data.materials.pop(index=2)                      # 先回到两槽
obj.data.materials.append(m_baked)                   # 再追加烘焙材质
obj.active_material_index = 2                        # active 指向它
print("  [旧做法] 追加槽+改active 后实际显示:", mt.visible_material_names(obj))
check("旧做法（只改 active）外观不变 —— 这正是之前的 bug",
      "Body" not in mt.visible_material_names(obj), mt.visible_material_names(obj))
obj.data.materials.pop(index=2)                      # 收回去

# ---- 用户定稿的流程：追加槽 → 分配面预览 → 收尾
ok, reason = mt.can_add_baked_slot(obj)
check("可以追加烘焙槽", ok, reason)
ok, reason = mt.add_baked_slot(obj, m_baked, fake_user_originals=True)
print("  add_baked_slot ->", ok, reason)
print("  槽:", [s.material.name for s in obj.material_slots])
print("  逐面索引（应该没动）:", [p.material_index for p in obj.data.polygons])
print("  实际显示:", mt.visible_material_names(obj))
check("追加成功", ok, reason)
check("**原槽一个不动，烘焙槽加在末尾**",
      [s.material.name for s in obj.material_slots] == ["OriginalA", "OriginalB", "Body"],
      [s.material.name for s in obj.material_slots])
check("**加槽本身不改外观**（用户要的是「自己分配才预览」）",
      set(mt.visible_material_names(obj)) == {"OriginalA", "OriginalB"},
      mt.visible_material_names(obj))
check("原材质数据块没被删",
      bpy.data.materials.get("OriginalA") and bpy.data.materials.get("OriginalB"))
check("原材质设了 fake_user",
      bpy.data.materials["OriginalA"].use_fake_user and bpy.data.materials["OriginalB"].use_fake_user)
check("烘焙材质也设了 fake_user（导出/保存后还在）", m_baked.use_fake_user)
check("多槽时记下了面索引 RLE",
      mt.compress_indices([0, 0, 1, 1, 1]) == "0:2,1:3", mt.compress_indices([0, 0, 1, 1, 1]))
check("RLE 能还原", mt.expand_indices("0:2,1:3") == [0, 0, 1, 1, 1])
check("已追加后不再重复追加", not mt.can_add_baked_slot(obj)[0])
check("记录了烘焙槽下标", mt.baked_slot_index(obj) == 2, mt.baked_slot_index(obj))

# ---- 分配面 = 预览
ok, reason = mt.assign_baked(obj)
print("  assign_baked ->", ok, reason)
print("  实际显示:", mt.visible_material_names(obj))
check("分配成功", ok, reason)
check("**分配之后外观真的变成烘焙材质**",
      mt.visible_material_names(obj) == ["Body"], mt.visible_material_names(obj))
ok, reason = mt.assign_original(obj)
check("能切回原材质", ok and set(mt.visible_material_names(obj)) == {"OriginalA", "OriginalB"},
      mt.visible_material_names(obj))
check("切回后逐面索引也还原了",
      [p.material_index for p in obj.data.polygons] == [0, 0, 0, 1, 1, 1],
      [p.material_index for p in obj.data.polygons])

# ---- 收尾：只留烘焙槽（导出就一个材质），而且**可逆**
mt.assign_baked(obj)
ok, reason = mt.finalize_for_export(obj)
print("  finalize ->", ok, reason)
print("  收尾后槽:", [s.material.name for s in obj.material_slots])
check("收尾成功", ok, reason)
check("收尾后只剩烘焙槽", [s.material.name for s in obj.material_slots] == ["Body"],
      [s.material.name for s in obj.material_slots])
check("收尾后逐面索引归 0",
      all(p.material_index == 0 for p in obj.data.polygons),
      [p.material_index for p in obj.data.polygons])
check("收尾后显示的就是烘焙材质",
      mt.visible_material_names(obj) == ["Body"], mt.visible_material_names(obj))

# ---- 恢复（收尾之后照样能还原 —— 备份存在物体上，不在槽里）
ok, reason = mt.restore_original_materials(obj)
print("  收尾后 restore ->", ok, reason)
print("  恢复后槽:", [s.material.name for s in obj.material_slots])
print("  恢复后逐面索引:", [p.material_index for p in obj.data.polygons])
check("收尾后仍能恢复成功", ok, reason)
check("槽顺序完全还原（收尾是可逆的）",
      [s.material.name for s in obj.material_slots] == ["OriginalA", "OriginalB"],
      [s.material.name for s in obj.material_slots])
check("**逐面材质索引也还原了**",
      [p.material_index for p in obj.data.polygons] == [0, 0, 0, 1, 1, 1],
      [p.material_index for p in obj.data.polygons])
check("外观回到两个原材质",
      set(mt.visible_material_names(obj)) == {"OriginalA", "OriginalB"})
check("恢复后清掉记录", not mt.has_baked_applied(obj))

# ---- 单槽物体（最常见情况）
bpy.ops.mesh.primitive_cube_add(size=1)
single = bpy.context.active_object
single.data.materials.append(bpy.data.materials.new("OnlyOne"))
mt.add_baked_slot(single, m_baked)
check("单槽物体也能追加烘焙槽",
      [s.material.name for s in single.material_slots] == ["OnlyOne", "Body"],
      [s.material.name for s in single.material_slots])
check("单槽物体也存了面索引（分配/还原要用）", mt.PROP_ORIG_INDICES in single)
mt.assign_baked(single)
check("单槽物体分配后显示烘焙材质",
      mt.visible_material_names(single) == ["Body"], mt.visible_material_names(single))
mt.restore_original_materials(single)
check("单槽恢复", [s.material.name for s in single.material_slots] == ["OnlyOne"],
      [s.material.name for s in single.material_slots])

# ---- 共享网格：现在**允许**（槽挂在 mesh 上，两个物体会一起变 —— Blender 的语义）
bpy.ops.mesh.primitive_cube_add(size=1)
shared_a = bpy.context.active_object
shared_a.data.materials.append(bpy.data.materials.new("Shared"))
shared_b = shared_a.copy()                      # obj.copy() 默认共享 mesh 数据
bpy.context.scene.collection.objects.link(shared_b)
print("  网格用户数:", shared_a.data.users, " 是否同一个 mesh:",
      shared_a.data is shared_b.data)
check("确实构造出了共享网格", shared_a.data.users > 1 and shared_a.data is shared_b.data,
      shared_a.data.users)
ok, reason = mt.can_add_baked_slot(shared_a)
check("共享网格可以被追加（旧版是拒绝的）", ok, reason)
mt.add_baked_slot(shared_a, m_baked)
check("共享网格的另一个物体也能看到这个槽（Blender 语义如此）",
      [s.material.name for s in shared_b.material_slots] == ["Shared", "Body"],
      [s.material.name for s in shared_b.material_slots])
check("第二个物体不会重复追加（共享网格上已经有这个材质了）",
      not mt.can_add_baked_slot(shared_b, m_baked)[0],
      mt.can_add_baked_slot(shared_b, m_baked))

# ---- 数据块被删掉时跳过而不是崩
bpy.ops.mesh.primitive_cube_add(size=1)
gone = bpy.context.active_object
gone.data.materials.append(bpy.data.materials.new("GoneA"))
gone.data.materials.append(bpy.data.materials.new("GoneB"))
mt.add_baked_slot(gone, m_baked)
bpy.data.materials.remove(bpy.data.materials["GoneB"])
ok, reason = mt.restore_original_materials(gone)
print("  删掉一个数据块后 restore ->", ok, reason)
check("数据块没了时跳过而不是崩", ok and "1 slot" in reason, reason)

print("\n================ RESULT ================")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for f in FAIL:
        print("  -", f)
else:
    print("ALL CHECKS PASSED")

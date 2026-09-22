# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   最终物体（第 4 页的可选动作）
#
#   复制物体 + 复制网格，放进目标集合。原物体一个字节都不动 ——
#   这样"建最终物体"是纯增量的，不满意直接删掉就行。
# ------------------------------------------------------------------------------------

import bpy

from ..core import naming


def create_final_object(obj, collection=None, suffix=" (Baked)", taken=None):
    """复制一个物体（连网格一起复制，避免共享网格导致改一个动全部）

    taken: 名字占用集合（set）。批量调用时必须复用同一个 set，
           否则同一批里两个重名副本会拿到同一个名字。
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return None, "not a mesh"

    duplicate = obj.copy()
    if obj.data is not None:
        duplicate.data = obj.data.copy()
    if taken is None:
        taken = {o.name for o in bpy.data.objects}
    else:
        taken.add(obj.name)
    duplicate.name = naming.unique_name("{}{}".format(obj.name, suffix), taken)
    if duplicate.data is not None:
        duplicate.data.name = duplicate.name

    target = collection or obj.users_collection[0] if obj.users_collection else None
    if target is None:
        target = bpy.context.scene.collection
    target.objects.link(duplicate)

    # 复制过来的物体不应该还挂着"已应用烘焙材质"的账 —— 它的槽就是它自己的
    for key in ("mbakery_baked_applied", "mbakery_orig_slots", "mbakery_orig_active",
                "mbakery_orig_indices", "mbakery_orig_polycount"):
        if key in duplicate:
            del duplicate[key]
    return duplicate, ""


def create_final_objects(objects, collection=None, suffix=" (Baked)"):
    """批量复制。返回 ([新物体], [(原名, 原因)])"""
    created = []
    skipped = []
    taken = {o.name for o in bpy.data.objects}
    for obj in objects:
        duplicate, reason = create_final_object(obj, collection, suffix, taken)
        if duplicate is None:
            skipped.append((getattr(obj, "name", "?"), reason))
        else:
            created.append(duplicate)
    return created, skipped


def default_collection(scene, name="Material Bakery"):
    """找（或建）一个用来放最终物体的集合"""
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        scene.collection.children.link(collection)
    return collection

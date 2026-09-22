# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   UDIM
#
#   一个物体跨多张 UV 瓦片时，贴图也要按瓦片切成多张。瓦片号不是简单的 1001+n：
#   它是**二维**编号，u 每跨 1 加 1，v 每跨 1 加 10：
#
#       v=1  ...  1011  1012  1013 ...
#       v=0  ...  1001  1002  1003 ...
#                 u=0   u=1   u=2
#
#   （早期是 1001..1010 一行排满再换行，现在 v 进位是 10，最多到 1099 + 1100 段，
#    这里按 Blender 现行规则实现。）
#
#   实现策略（也是 Blender 自己的做法）：
#       建一张 TILED 图像，把用到的瓦片都 new 出来，然后**一次烘焙**，
#       Cycles 会按 UV 落进对应瓦片。导出时文件名里带 <UDIM>，
#       image.save() 会一次写出所有瓦片各自的文件。
# ------------------------------------------------------------------------------------

import math
import os

import bpy

from .. import compat
from ..core import naming


UDIM_START = 1001
UDIM_TOKEN = "<UDIM>"
# UV 刚好落在整数边界上时算**左边/下边**那块瓦片。
# 没有这个 epsilon，一个 UV 铺满 0..1 的普通四边形（= 绝大多数模型）会同时
# 报出 1001/1002/1011/1012 四张瓦片，于是"任何物体都是 UDIM"。
TILE_EPSILON = 1e-5


def _axis_index(value, epsilon=TILE_EPSILON):
    """UV 某一个轴上的瓦片下标"""
    if value < 0:
        return 0
    index = int(math.floor(value))
    if index > 0 and (value - index) < epsilon:
        index -= 1
    return index


def tile_of(u, v):
    """UV 坐标 -> 瓦片号"""
    return UDIM_START + _axis_index(u) + _axis_index(v) * 10


def tile_bounds(tile):
    """瓦片号 -> (u0, v0, u1, v1)"""
    offset = tile - UDIM_START
    column = offset % 10
    row = offset // 10
    return float(column), float(row), float(column + 1), float(row + 1)


def object_tiles(obj, tolerance=0.0):
    """一个物体用到的所有瓦片号（升序）

    ⚠ 按**面的 UV 包围盒**扫，不能只看顶点。
      一个从 (0,0) 铺到 (1,3) 的四边形只带 4 个顶点，但它在 UV 空间里
      实实在在横跨 1001 / 1011 / 1021 三张瓦片 —— 只看顶点会把中间那张漏掉。
      凹多边形可能多算一两张，宁可多烘也不要漏。
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return []
    layer = None
    for candidate in obj.data.uv_layers:
        if candidate.active_render:
            layer = candidate
            break
    layer = layer or obj.data.uv_layers.active
    if layer is None:
        return []

    tiles = set()
    for polygon in obj.data.polygons:
        us = []
        vs = []
        for loop_index in polygon.loop_indices:
            u, v = layer.data[loop_index].uv
            us.append(u)
            vs.append(v)
        if not us:
            continue
        column_0 = _axis_index(min(us))
        column_1 = _axis_index(max(us))
        row_0 = _axis_index(min(vs))
        row_1 = _axis_index(max(vs))
        for row in range(row_0, row_1 + 1):
            for column in range(column_0, column_1 + 1):
                tiles.add(UDIM_START + column + row * 10)
    return sorted(tiles)


def group_tiles(objects):
    """一组物体用到的全部瓦片号"""
    tiles = set()
    for obj in objects:
        tiles.update(object_tiles(obj))
    return sorted(tiles)


def is_udim(objects):
    """这组物体是不是跨了多张瓦片"""
    return len(group_tiles(objects)) > 1


def tiles_of_plan(plan):
    """{组名: [瓦片号, ...]}"""
    result = {}
    for group in plan.groups:
        tiles = group_tiles(group.objects)
        if tiles:
            result[group.name] = tiles
    return result


# ------------------------------------------------------------------------------------
#   图像

def seed_tile_files(directory, image_name, size, tiles, channel, file_format='PNG'):
    """先在磁盘上把瓦片文件造出来，返回带 <UDIM> 的路径。

    ⚠ 为什么非得这么绕：**Cycles 无法烘焙进一张"纯内存"的 tiled 图像**。
      实测 bpy.data.images.new(tiled=True) 造出来的图，无论怎么逐瓦片填像素
      （哪怕 has_data 已经是 True），烘焙都会报
      `Uninitialized image "<名字>" from object "<物体>"`。
      只有**磁盘上有真实文件**、再用 image.open(use_udim_detecting=True) 加载出来的
      tiled 图像才烘得动。所以这里先写占位文件，再把它们作为一个 UDIM 集打开。
    """
    os.makedirs(directory, exist_ok=True)
    token_path = ""
    for tile in tiles:
        path = os.path.join(directory, "{}.{}".format(
            udim_filename(image_name, tile), _extension(file_format)))
        if not os.path.exists(path):
            _write_flat_file(path, size, channel, file_format)
        if not token_path:
            token_path = path.replace(str(tile), UDIM_TOKEN)
    return token_path


def _extension(file_format):
    from .. import compat
    return compat.file_extension(file_format)


def _write_flat_file(path, size, channel, file_format):
    """写一张纯色占位图"""
    flat = bpy.data.images.new("__mbakery_seed", width=int(size), height=int(size),
                               alpha=False, float_buffer=channel.is_data)
    try:
        flat.pixels.foreach_set([0.0] * (int(size) * int(size) * 4))
        flat.filepath_raw = path
        flat.file_format = file_format
        flat.save()
    finally:
        bpy.data.images.remove(flat)


def ensure_tiled_image(name, size, tiles, channel, file_format='PNG',
                       reuse=True, directory=""):
    """取得（或重建）一张可烘焙的 UDIM 瓦片图"""
    from ..engine.backend import IMAGE_TAG

    wanted = sorted(int(t) for t in tiles)
    existing = bpy.data.images.get(name)
    if existing is not None and reuse and existing.source == 'TILED':
        if sorted(t.number for t in existing.tiles) == wanted:
            return existing
    if existing is not None:
        bpy.data.images.remove(existing)

    if not directory:
        raise RuntimeError(
            "UDIM needs a folder to keep the tile files in — set an output folder")

    token = seed_tile_files(directory, name, size, wanted, channel, file_format)
    before = set(bpy.data.images)
    bpy.ops.image.open(filepath=token, use_udim_detecting=True)
    opened = [image for image in bpy.data.images if image not in before]

    image = opened[0] if opened else bpy.data.images.get(token)
    if image is None:
        raise RuntimeError("could not open the UDIM tile set at {}".format(token))

    image.name = name
    image[IMAGE_TAG] = 1
    image.file_format = file_format
    image.colorspace_settings.name = compat.safe_colorspace_name(channel.colorspace)
    if not image.tiles:
        raise RuntimeError("Blender did not recognise {} as a UDIM set".format(token))
    return image


def tile_uv_shift(tile):
    """烘这张瓦片时要把 UV 平移多少，才能让它落到 0..1。

    1002 -> (-1, 0)；1011 -> (0, -1)；1033 -> (-2, -3)。
    """
    if not tile:
        return ()
    offset = tile - UDIM_START
    return (-(offset % 10), -(offset // 10))


def udim_filename(image_name, tile):
    """把 <UDIM> 换成真实瓦片号；没有 token 就在后面补上"""
    if UDIM_TOKEN in image_name:
        return image_name.replace(UDIM_TOKEN, str(tile))
    return "{}.{}".format(image_name, tile)


def export_tiles(image, directory, image_name, file_format='PNG'):
    """写出一张 TILED 图像的所有瓦片。返回 [(瓦片号, 路径), ...]

    ⚠ 不用 Blender 自己的"文件名带 <UDIM> 一次 save 全部写出"那条路 ——
      实测在 --background 下直接失败（"could not be saved to ...<UDIM>.png"），
      而且 pack() 也会因为瓦片文件还不存在而失败。
      改成：逐个瓦片把像素拷进一张普通图再存。多一次内存拷贝，
      但行为完全确定，前台后台一致。
    """
    from ..engine.backend import image_filepath

    written = []
    if image is None or not image.tiles:
        return written

    width, height = image.size
    original = image.tiles.active
    try:
        for tile in list(image.tiles):
            number = tile.number
            name = udim_filename(image_name, number)
            path = image_filepath(directory, name, file_format)

            # ⚠ 切瓦片要用 tiles.active = tile（不是只设 active_index）。
            #   只改 active_index 的话 image.pixels 读到的仍是原来那张瓦片的缓冲区，
            #   导出的就会是一堆黑图 —— 烘焙其实成功了，是读错了。
            image.tiles.active = tile
            image.update()
            pixels = list(image.pixels)

            flat = bpy.data.images.new("__mbakery_tile", width=width, height=height,
                                       alpha=False, float_buffer=image.is_float)
            try:
                flat.pixels.foreach_set(pixels)
                flat.colorspace_settings.name = image.colorspace_settings.name
                flat.filepath_raw = path
                flat.file_format = file_format
                flat.save()
                written.append((number, path))
            finally:
                bpy.data.images.remove(flat)
    finally:
        if image.tiles:
            image.tiles.active = original
    return written

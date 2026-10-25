# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   UV 重叠预检
#
#   把 UV 三角形光栅化到一张 resolution×resolution 的格子上：
#       覆盖像素 = 至少被一个面盖到
#       重叠像素 = 被两个及以上面盖到
#
#   这**只用来提示**。重叠不等于错 —— 对称模型复用 UV 是常规做法，
#   所以永远只报警，不阻止烘焙。
# ------------------------------------------------------------------------------------

DEFAULT_RESOLUTION = 128


class OverlapStats:
    __slots__ = ("object_name", "faces", "covered", "overlap", "resolution")

    def __init__(self, object_name, faces, covered, overlap, resolution):
        self.object_name = object_name
        self.faces = faces
        self.covered = covered
        self.overlap = overlap
        self.resolution = resolution

    @property
    def overlap_ratio(self):
        return self.overlap / float(self.covered) if self.covered else 0.0

    @property
    def covered_ratio(self):
        total = self.resolution * self.resolution
        return self.covered / float(total) if total else 0.0

    def message(self):
        return "{}: {:.1f}% of the UV area is overlapped ({} of {} texels)".format(
            self.object_name, self.overlap_ratio * 100.0, self.overlap, self.covered)

    def to_dict(self):
        return {
            "object": self.object_name,
            "faces": self.faces,
            "covered": self.covered,
            "overlap": self.overlap,
            "resolution": self.resolution,
        }

    def __repr__(self):
        return "<OverlapStats {} {:.3f}>".format(self.object_name, self.overlap_ratio)


def scan_object(obj, resolution=DEFAULT_RESOLUTION):
    """测一个物体的 UV 重叠。没有 UV 或者没有面时返回 None。"""
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return None
    mesh = obj.data
    uv_layer = mesh.uv_layers.active
    if uv_layer is None or not mesh.polygons:
        return None

    resolution = max(8, int(resolution))
    counts = bytearray(resolution * resolution)
    faces = _rasterize(counts, resolution, mesh, uv_layer)
    if not faces:
        return None
    return _stats(obj.name, faces, counts, resolution)


def scan_together(objects, resolution=DEFAULT_RESOLUTION, label="Group"):
    """把一组物体**画到同一张格子**上再测 —— 这才是「共用一张贴图」要看的东西

    单独测每个物体都是零重叠，不代表它们烘进同一张图不打架。
    """
    if not objects:
        return None
    resolution = max(8, int(resolution))
    counts = bytearray(resolution * resolution)
    total_faces = 0
    used = 0
    for obj in objects:
        if obj is None or obj.type != 'MESH' or obj.data is None:
            continue
        mesh = obj.data
        layer = None
        for candidate in mesh.uv_layers:
            if candidate.active_render:
                layer = candidate
                break
        layer = layer or mesh.uv_layers.active
        if layer is None:
            continue
        faces = _rasterize(counts, resolution, mesh, layer)
        if faces:
            total_faces += faces
            used += 1
    if not used:
        return None
    return _stats(label, total_faces, counts, resolution)


def _rasterize(counts, resolution, mesh, uv_layer):
    """把一个网格的 UV 叠到 counts 上，返回画了几个面"""
    coords = [(uv_layer.data[i].uv[0], uv_layer.data[i].uv[1])
              for i in range(len(uv_layer.data))]
    faces = 0
    for polygon in mesh.polygons:
        indices = list(polygon.loop_indices)
        if len(indices) < 3:
            continue
        _fill_polygon(counts, resolution, [coords[i] for i in indices])
        faces += 1
    return faces


def _stats(name, faces, counts, resolution):
    covered = 0
    overlap = 0
    for value in counts:
        if value:
            covered += 1
            if value > 1:
                overlap += 1
    return OverlapStats(name, faces, covered, overlap, resolution)


def scan_objects(objects, resolution=DEFAULT_RESOLUTION):
    results = []
    for obj in objects:
        stats = scan_object(obj, resolution)
        if stats is not None:
            results.append(stats)
    return results


def worst(objects, resolution=DEFAULT_RESOLUTION, threshold=0.001):
    """重叠最严重的那个（低于阈值就当没有）"""
    stats = scan_objects(objects, resolution)
    stats = [s for s in stats if s.overlap_ratio > threshold]
    if not stats:
        return None
    stats.sort(key=lambda s: s.overlap_ratio, reverse=True)
    return stats[0]


# ------------------------------------------------------------------------------------
#   光栅化
#
#   整个多边形一次性光栅化（奇偶射线法），**不是**先三角化再逐个三角形填。
#   三角化会在扇形对角线那条内部边上重复计入边界像素 —— 一个本身完全不重叠的
#   四边形会被报成 1.6% 重叠。按多边形填就没有内部边，不存在这个问题。

def _fill_polygon(counts, resolution, points):
    xs = [p[0] * resolution for p in points]
    ys = [p[1] * resolution for p in points]

    min_x = max(0, int(min(xs)))
    max_x = min(resolution - 1, int(max(xs)) + 1)
    min_y = max(0, int(min(ys)))
    max_y = min(resolution - 1, int(max(ys)) + 1)
    if min_x > max_x or min_y > max_y:
        return                                   # 整个多边形在 0..1 之外

    count = len(points)
    if count < 3:
        return

    for y in range(min_y, max_y + 1):
        py = y + 0.5
        row = y * resolution
        for x in range(min_x, max_x + 1):
            px = x + 0.5
            inside = False
            j = count - 1
            for i in range(count):
                yi = ys[i]
                yj = ys[j]
                if (yi > py) != (yj > py):
                    cross = (xs[j] - xs[i]) * (py - yi) / (yj - yi) + xs[i]
                    if px < cross:
                        inside = not inside
                j = i
            if inside:
                index = row + x
                if counts[index] < 255:
                    counts[index] += 1

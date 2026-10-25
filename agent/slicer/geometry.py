import math

import numpy

DOWN = numpy.array([0.0, 0.0, -1.0])

AXIS_DIRECTIONS = (
    [0, 0, -1],
    [0, 0, 1],
    [1, 0, 0],
    [-1, 0, 0],
    [0, 1, 0],
    [0, -1, 0],
)

ON_BED_COSINE = 0.985
ON_BED_TOLERANCE_MM = 0.05
MIN_OVERHANG_HEIGHT_MM = 0.1

NORMAL_GROUPING_STEP = 0.08
MAX_FACE_CLUSTERS = 14
SAME_DIRECTION_COSINE = 0.998

AXISYMMETRY_SLICES = 8
AXISYMMETRY_SECTORS = 24
AXISYMMETRY_MIN_FILLED_SHARE = 0.75
AXISYMMETRIC_MAX_CV = 0.06
HOLLOW_MIN_INNER_RATIO = 0.4

MAX_FACES_FOR_TOPOLOGY = 400000


def scene_to_plate(points):
    points = numpy.asarray(points, dtype=numpy.float64)
    plate_points = numpy.empty_like(points)
    plate_points[..., 0] = points[..., 0]
    plate_points[..., 1] = -points[..., 2]
    plate_points[..., 2] = points[..., 1]
    return plate_points


def indexed_triangles(vertices, indices):
    """Собирает треугольники (F, 3, 3) из вершин сетки и индекса."""
    if vertices is None:
        return None
    vertices = numpy.asarray(vertices, dtype=numpy.float64)
    if vertices.size == 0:
        return None
    if indices is not None:
        return vertices[numpy.asarray(indices)]
    if len(vertices) % 3 == 0:
        return vertices.reshape(-1, 3, 3)
    return None


def transform_triangles(triangles, matrix):
    """Переводит треугольники матрицей 4×4 (мировое преобразование сцены)."""
    matrix = numpy.asarray(matrix, dtype=numpy.float64)
    points = triangles.reshape(-1, 3)
    world_points = points.dot(matrix[:3, :3].T) + matrix[:3, 3]
    return world_points.reshape(-1, 3, 3)


def plate_to_scene_vector(vector):
    return (float(vector[0]), float(vector[2]), -float(vector[1]))


def face_data(triangles):
    triangles = numpy.asarray(triangles, dtype=numpy.float64)
    edge_a = triangles[:, 1] - triangles[:, 0]
    edge_b = triangles[:, 2] - triangles[:, 0]
    cross = numpy.cross(edge_a, edge_b)
    double_areas = numpy.linalg.norm(cross, axis=1)

    valid = double_areas > 1e-12
    triangles = triangles[valid]
    normals = cross[valid] / double_areas[valid][:, None]
    areas = 0.5 * double_areas[valid]
    return triangles, normals, areas


def mesh_volume(triangles):
    cross = numpy.cross(triangles[:, 1], triangles[:, 2])
    signed_volume = numpy.einsum("ij,ij->i", triangles[:, 0], cross).sum() / 6.0
    return abs(float(signed_volume))


def mesh_topology(triangles, max_faces=MAX_FACES_FOR_TOPOLOGY):
    if len(triangles) > max_faces:
        return None

    vertices = numpy.round(triangles.reshape(-1, 3), 4)
    try:
        _, vertex_ids = numpy.unique(vertices, axis=0, return_inverse=True)
    except TypeError:
        return None

    faces = numpy.asarray(vertex_ids).reshape(-1, 3)
    edges = numpy.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges = numpy.sort(edges, axis=1)
    _, edge_usage = numpy.unique(edges, axis=0, return_counts=True)

    open_edges = int(numpy.sum(edge_usage == 1))
    nonmanifold_edges = int(numpy.sum(edge_usage > 2))
    return {
        "watertight": open_edges == 0 and nonmanifold_edges == 0,
        "open_edges": open_edges,
        "nonmanifold_edges": nonmanifold_edges,
    }


def _perpendicular_basis(axis):
    if abs(axis[0]) < 0.9:
        helper = numpy.array([1.0, 0.0, 0.0])
    else:
        helper = numpy.array([0.0, 1.0, 0.0])

    first = numpy.cross(axis, helper)
    first /= numpy.linalg.norm(first)
    second = numpy.cross(axis, first)
    return first, second


def _principal_axes(triangles, areas):
    centroids = triangles.mean(axis=1)
    weights = areas / max(areas.sum(), 1e-12)
    center = (centroids * weights[:, None]).sum(axis=0)
    offsets = centroids - center
    covariance = (offsets * weights[:, None]).T.dot(offsets)
    _, eigenvectors = numpy.linalg.eigh(covariance)
    return center, eigenvectors


def _radial_profile(points, axis):
    heights = points.dot(axis)
    radial = points - numpy.outer(heights, axis)
    radii = numpy.linalg.norm(radial, axis=1)

    first, second = _perpendicular_basis(axis)
    angles = numpy.arctan2(radial.dot(second), radial.dot(first))
    return heights, radii, angles


def _evaluate_axis(points, axis):
    heights, radii, angles = _radial_profile(points, axis)
    length = heights.max() - heights.min()
    if length < 1e-6:
        return None

    slices = AXISYMMETRY_SLICES
    sectors = AXISYMMETRY_SECTORS
    slice_index = ((heights - heights.min()) / length * slices).astype(int)
    slice_index = numpy.clip(slice_index, 0, slices - 1)
    sector_index = ((angles + math.pi) / (2 * math.pi) * sectors).astype(int)
    sector_index = numpy.clip(sector_index, 0, sectors - 1)

    max_radius = numpy.zeros((slices, sectors))
    min_radius = numpy.full((slices, sectors), numpy.inf)
    numpy.maximum.at(max_radius, (slice_index, sector_index), radii)
    numpy.minimum.at(min_radius, (slice_index, sector_index), radii)

    variations = []
    inner_ratios = []
    for slice_number in range(slices):
        outer = max_radius[slice_number]
        filled = outer > 0
        if filled.sum() < sectors * AXISYMMETRY_MIN_FILLED_SHARE:
            continue

        mean_outer = outer[filled].mean()
        if mean_outer < 1e-6:
            continue

        variations.append(outer[filled].std() / mean_outer)
        inner = min_radius[slice_number][filled]
        inner_ratios.append(float(numpy.median(inner) / mean_outer))

    if not variations:
        return None

    inner_radius_ratio = None
    if inner_ratios:
        inner_radius_ratio = round(float(numpy.median(inner_ratios)), 2)

    return {
        "axis": [round(float(component), 3) for component in axis],
        "radial_cv": round(float(numpy.median(variations)), 3),
        "length_mm": round(float(length), 2),
        "max_radius_mm": round(float(radii.max()), 2),
        "inner_radius_ratio": inner_radius_ratio,
    }


def axisymmetry(triangles, areas):
    center, eigenvectors = _principal_axes(triangles, areas)
    points = triangles.reshape(-1, 3) - center

    best = None
    for index in range(3):
        axis = eigenvectors[:, index] / numpy.linalg.norm(eigenvectors[:, index])
        candidate = _evaluate_axis(points, axis)
        if candidate is None:
            continue
        if best is None or candidate["radial_cv"] < best["radial_cv"]:
            best = candidate

    if best is None:
        return {"axisymmetric": False, "radial_cv": None}

    best["axisymmetric"] = best["radial_cv"] < AXISYMMETRIC_MAX_CV
    inner_ratio = best.get("inner_radius_ratio")
    best["hollow"] = bool(best["axisymmetric"] and inner_ratio and inner_ratio > HOLLOW_MIN_INNER_RATIO)
    return best


def evaluate_orientation(triangles, normals, areas, down, support_angle):
    down = numpy.asarray(down, dtype=numpy.float64)
    down = down / numpy.linalg.norm(down)

    facing_down = normals.dot(down)
    depth = triangles.dot(down)
    bed_level = depth.max()
    face_height = bed_level - depth.mean(axis=1)
    face_top = bed_level - depth.min(axis=1)

    on_bed = (facing_down > ON_BED_COSINE) & (face_top < ON_BED_TOLERANCE_MM)
    overhang_threshold = math.sin(math.radians(support_angle))
    overhang = (facing_down > overhang_threshold) & ~on_bed & (face_height > MIN_OVERHANG_HEIGHT_MM)

    projected_areas = areas * facing_down
    support_volume = float((projected_areas[overhang] * face_height[overhang]).sum())

    return {
        "height_mm": round(float(bed_level - depth.min()), 2),
        "bed_contact_mm2": round(float(areas[on_bed].sum()), 1),
        "overhang_area_mm2": round(float(areas[overhang].sum()), 1),
        "support_volume_est_mm3": round(support_volume, 1),
    }


def rotation_to_down(down):
    direction = numpy.asarray(down, dtype=numpy.float64)
    direction = direction / numpy.linalg.norm(direction)

    axis = numpy.cross(direction, DOWN)
    sin_angle = numpy.linalg.norm(axis)
    cos_angle = float(direction.dot(DOWN))

    if sin_angle < 1e-9:
        if cos_angle > 0:
            return [1.0, 0.0, 0.0], 0.0
        return [1.0, 0.0, 0.0], 180.0

    axis = axis / sin_angle
    angle = math.degrees(math.atan2(sin_angle, cos_angle))
    return [float(component) for component in axis], angle


def _largest_face_directions(normals, areas, max_clusters):
    grouped_normals = numpy.round(normals / NORMAL_GROUPING_STEP).astype(numpy.int64)
    try:
        _, cluster_ids = numpy.unique(grouped_normals, axis=0, return_inverse=True)
    except TypeError:
        return []

    cluster_ids = numpy.asarray(cluster_ids).reshape(-1)
    cluster_areas = numpy.bincount(cluster_ids, weights=areas)
    largest_clusters = numpy.argsort(cluster_areas)[::-1][:max_clusters]

    directions = []
    for cluster in largest_clusters:
        in_cluster = cluster_ids == cluster
        weighted_normal = (normals[in_cluster] * areas[in_cluster][:, None]).sum(axis=0)
        length = numpy.linalg.norm(weighted_normal)
        if length > 1e-9:
            directions.append(weighted_normal / length)
    return directions


def _candidate_directions(normals, areas, max_clusters=MAX_FACE_CLUSTERS):
    directions = [numpy.array(direction, dtype=numpy.float64) for direction in AXIS_DIRECTIONS]
    directions.extend(_largest_face_directions(normals, areas, max_clusters))

    unique_directions = []
    for direction in directions:
        is_new = True
        for known in unique_directions:
            if direction.dot(known) >= SAME_DIRECTION_COSINE:
                is_new = False
                break
        if is_new:
            unique_directions.append(direction)
    return unique_directions


def _score(metrics, volume, max_dimension, total_area):
    support_share = metrics["support_volume_est_mm3"] / max(volume, 1.0)
    height_share = metrics["height_mm"] / max(max_dimension, 1.0)
    contact_share = min(metrics["bed_contact_mm2"] / max(total_area * 0.05, 1.0), 1.0)

    # Приоритет: минимум поддержек + минимальная высота Z (деталь лежит плашмя) + максимальный контакт со столом
    score = support_share + 0.6 * height_share - 0.8 * contact_share
    if metrics["bed_contact_mm2"] < 25.0:
        score += 1.0
    return score


def _size_dict(size):
    return {
        "x": round(float(size[0]), 2),
        "y": round(float(size[1]), 2),
        "z": round(float(size[2]), 2),
    }


def _hints(size, volume, current, mesh, axisymmetry_info):
    hints = []

    if mesh is not None and not mesh["watertight"]:
        hints.append("Сетка не замкнута (%d открытых рёбер) — возможны дефекты слайсинга." % mesh["open_edges"])

    if axisymmetry_info.get("axisymmetric"):
        hint = "Модель близка к телу вращения (ось %s) — кандидат на 5D-режим." % axisymmetry_info["axis"]
        if axisymmetry_info.get("hollow"):
            hint += " Полая — похожа на трубу/оболочку."
        hints.append(hint)

    footprint = max(size[0] * size[1], 1.0)
    if size[2] > 3 * math.sqrt(footprint):
        hints.append("Высокая и узкая в текущей ориентации — риск опрокидывания, нужен brim или другая ориентация.")

    if current["bed_contact_mm2"] < 25:
        hints.append("Маленькая площадь контакта со столом в текущей ориентации.")

    if current["support_volume_est_mm3"] > 0.3 * max(volume, 1.0):
        hints.append("Много нависаний в текущей ориентации — проверьте suggest_orientations.")

    smallest_dimension = float(min(size))
    if smallest_dimension < 2.0:
        hints.append(
            "Есть очень тонкий габарит (%.2f мм) — проверьте ширину линии и число стенок." % smallest_dimension
        )

    return hints


def analyze(triangles, support_angle=50.0):
    triangles, normals, areas = face_data(triangles)
    if len(triangles) == 0:
        raise ValueError("Пустая сетка")

    points = triangles.reshape(-1, 3)
    size = points.max(axis=0) - points.min(axis=0)
    volume = mesh_volume(triangles)
    current = evaluate_orientation(triangles, normals, areas, DOWN, support_angle)
    mesh = mesh_topology(triangles)
    axisymmetry_info = axisymmetry(triangles, areas)

    return {
        "triangles": int(len(triangles)),
        "size_mm": _size_dict(size),
        "volume_cm3": round(volume / 1000.0, 3),
        "surface_area_cm2": round(float(areas.sum()) / 100.0, 2),
        "support_angle": support_angle,
        "current_orientation": current,
        "mesh": mesh,
        "axisymmetry": axisymmetry_info,
        "hints": _hints(size, volume, current, mesh, axisymmetry_info),
    }


def suggest_orientations(triangles, support_angle=50.0, top_n=5):
    triangles, normals, areas = face_data(triangles)
    if len(triangles) == 0:
        raise ValueError("Пустая сетка")

    points = triangles.reshape(-1, 3)
    max_dimension = float((points.max(axis=0) - points.min(axis=0)).max())
    volume = mesh_volume(triangles)
    total_area = float(areas.sum())
    current = evaluate_orientation(triangles, normals, areas, DOWN, support_angle)

    candidates = []
    for direction in _candidate_directions(normals, areas):
        metrics = evaluate_orientation(triangles, normals, areas, direction, support_angle)
        axis, angle = rotation_to_down(direction)
        candidates.append({
            "down": [round(float(component), 4) for component in direction],
            "rotation": {
                "axis": [round(component, 4) for component in axis],
                "angle_deg": round(angle, 1),
            },
            "is_current": bool(direction.dot(DOWN) > SAME_DIRECTION_COSINE),
            "metrics": metrics,
            "score": round(_score(metrics, volume, max_dimension, total_area), 4),
        })

    candidates.sort(key=lambda candidate: candidate["score"])

    current_support = current["support_volume_est_mm3"]
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
        if current_support > 1:
            candidate_support = candidate["metrics"]["support_volume_est_mm3"]
            change = (candidate_support - current_support) / current_support * 100.0
            candidate["support_change_vs_current_pct"] = round(change, 1)

    return {
        "current": current,
        "support_angle": support_angle,
        "candidates": candidates[:max(1, int(top_n))],
        "note": (
            "Объём поддержек — оценка (проекция нависаний до стола). Ориентацию стоит выбирать "
            "и с учётом нагрузки: слои не должны работать на отрыв."
        ),
    }

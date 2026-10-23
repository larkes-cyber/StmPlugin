# -*- coding: utf-8 -*-
"""Анализ геометрии на numpy (без внешних зависимостей).

Все функции работают в системе координат стола: Z — вверх, единицы — мм.
Сцена Cura использует Y-up, перевод — scene_to_plate / plate_to_scene_vector.
Треугольники передаются массивом формы (F, 3, 3).
"""
import math

import numpy

DOWN = numpy.array([0.0, 0.0, -1.0])


def scene_to_plate(points):
    points = numpy.asarray(points, dtype=numpy.float64)
    out = numpy.empty_like(points)
    out[..., 0] = points[..., 0]
    out[..., 1] = -points[..., 2]
    out[..., 2] = points[..., 1]
    return out


def plate_to_scene_vector(v):
    return (float(v[0]), float(v[2]), -float(v[1]))


def face_data(tris):
    tris = numpy.asarray(tris, dtype=numpy.float64)
    cross = numpy.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    double_area = numpy.linalg.norm(cross, axis=1)
    valid = double_area > 1e-12
    tris = tris[valid]
    normals = cross[valid] / double_area[valid][:, None]
    return tris, normals, 0.5 * double_area[valid]


def mesh_volume(tris):
    signed = numpy.einsum("ij,ij->i", tris[:, 0], numpy.cross(tris[:, 1], tris[:, 2])).sum() / 6.0
    return abs(float(signed))


def mesh_topology(tris, max_faces=400000):
    """Замкнутость сетки: каждое ребро должно принадлежать ровно двум треугольникам."""
    if len(tris) > max_faces:
        return None
    verts = numpy.round(tris.reshape(-1, 3), 4)
    try:
        _, inverse = numpy.unique(verts, axis=0, return_inverse=True)
    except TypeError:  # очень старый numpy
        return None
    faces = numpy.asarray(inverse).reshape(-1, 3)
    edges = numpy.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges = numpy.sort(edges, axis=1)
    _, counts = numpy.unique(edges, axis=0, return_counts=True)
    open_edges = int(numpy.sum(counts == 1))
    nonmanifold = int(numpy.sum(counts > 2))
    return {"watertight": open_edges == 0 and nonmanifold == 0,
            "open_edges": open_edges, "nonmanifold_edges": nonmanifold}


def _orthonormal(axis):
    helper = numpy.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else numpy.array([0.0, 1.0, 0.0])
    u = numpy.cross(axis, helper)
    u /= numpy.linalg.norm(u)
    return u, numpy.cross(axis, u)


def axisymmetry(tris, area):
    """Насколько модель похожа на тело вращения (кандидат на 5D-печать на поворотной оси).

    Для каждой главной оси инерции режем модель на слои по высоте и смотрим, насколько
    постоянен максимальный радиус по углу. radial_cv ≈ 0 — идеальное тело вращения.
    """
    centroids = tris.mean(axis=1)
    weights = area / max(area.sum(), 1e-12)
    center = (centroids * weights[:, None]).sum(axis=0)
    X = centroids - center
    cov = (X * weights[:, None]).T.dot(X)
    _, eigvecs = numpy.linalg.eigh(cov)
    points = tris.reshape(-1, 3) - center

    best = None
    for i in range(3):
        axis = eigvecs[:, i] / numpy.linalg.norm(eigvecs[:, i])
        h = points.dot(axis)
        radial = points - numpy.outer(h, axis)
        r = numpy.linalg.norm(radial, axis=1)
        u, v = _orthonormal(axis)
        angle = numpy.arctan2(radial.dot(v), radial.dot(u))
        span = h.max() - h.min()
        if span < 1e-6:
            continue
        slices, sectors = 8, 24
        hb = numpy.clip(((h - h.min()) / span * slices).astype(int), 0, slices - 1)
        ab = numpy.clip(((angle + math.pi) / (2 * math.pi) * sectors).astype(int), 0, sectors - 1)
        rmax = numpy.zeros((slices, sectors))
        rmin = numpy.full((slices, sectors), numpy.inf)
        numpy.maximum.at(rmax, (hb, ab), r)
        numpy.minimum.at(rmin, (hb, ab), r)
        cvs, inner = [], []
        for s in range(slices):
            row = rmax[s]
            filled = row > 0
            if filled.sum() < sectors * 0.75 or row[filled].mean() < 1e-6:
                continue
            cvs.append(row[filled].std() / row[filled].mean())
            inner_row = rmin[s][filled]
            inner.append(float(numpy.median(inner_row) / row[filled].mean()))
        if not cvs:
            continue
        cv = float(numpy.median(cvs))
        if best is None or cv < best["radial_cv"]:
            best = {"axis": [round(float(c), 3) for c in axis], "radial_cv": round(cv, 3),
                    "length_mm": round(float(span), 2), "max_radius_mm": round(float(r.max()), 2),
                    "inner_radius_ratio": round(float(numpy.median(inner)), 2) if inner else None}
    if best is None:
        return {"axisymmetric": False, "radial_cv": None}
    best["axisymmetric"] = best["radial_cv"] < 0.06
    best["hollow"] = bool(best["axisymmetric"] and best.get("inner_radius_ratio") and best["inner_radius_ratio"] > 0.4)
    return best


def evaluate_orientation(tris, normals, area, down, support_angle):
    """Оценка ориентации, при которой направление down смотрит на стол."""
    down = numpy.asarray(down, dtype=numpy.float64)
    down = down / numpy.linalg.norm(down)
    cos_down = normals.dot(down)
    depth = tris.dot(down)                    # чем больше, тем ниже точка
    bed = depth.max()
    face_height = bed - depth.mean(axis=1)    # высота центра грани над столом
    face_top = bed - depth.min(axis=1)
    on_bed = (cos_down > 0.985) & (face_top < 0.05)
    threshold = math.sin(math.radians(support_angle))
    overhang = (cos_down > threshold) & ~on_bed & (face_height > 0.1)
    projected = area * cos_down
    support_volume = float((projected[overhang] * face_height[overhang]).sum())
    return {
        "height_mm": round(float(bed - depth.min()), 2),
        "bed_contact_mm2": round(float(area[on_bed].sum()), 1),
        "overhang_area_mm2": round(float(area[overhang].sum()), 1),
        "support_volume_est_mm3": round(support_volume, 1),
    }


def rotation_to_down(down):
    """Ось и угол поворота (система стола), переводящие вектор down в (0,0,-1)."""
    d = numpy.asarray(down, dtype=numpy.float64)
    d = d / numpy.linalg.norm(d)
    axis = numpy.cross(d, DOWN)
    s = numpy.linalg.norm(axis)
    c = float(d.dot(DOWN))
    if s < 1e-9:
        if c > 0:
            return [1.0, 0.0, 0.0], 0.0
        return [1.0, 0.0, 0.0], 180.0
    axis = axis / s
    return [float(a) for a in axis], math.degrees(math.atan2(s, c))


def _candidate_directions(normals, area, max_clusters=14):
    dirs = [numpy.array(v, dtype=numpy.float64) for v in
            ([0, 0, -1], [0, 0, 1], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0])]
    keys = numpy.round(normals / 0.08).astype(numpy.int64)
    try:
        _, inverse = numpy.unique(keys, axis=0, return_inverse=True)
        inverse = numpy.asarray(inverse).reshape(-1)
        cluster_area = numpy.bincount(inverse, weights=area)
        for cluster in numpy.argsort(cluster_area)[::-1][:max_clusters]:
            mask = inverse == cluster
            mean = (normals[mask] * area[mask][:, None]).sum(axis=0)
            norm = numpy.linalg.norm(mean)
            if norm > 1e-9:
                dirs.append(mean / norm)
    except TypeError:
        pass
    unique = []
    for d in dirs:
        if all(d.dot(u) < 0.998 for u in unique):
            unique.append(d)
    return unique


def _score(metrics, volume, max_dim, total_area):
    support_n = metrics["support_volume_est_mm3"] / max(volume, 1.0)
    height_n = metrics["height_mm"] / max(max_dim, 1.0)
    contact_n = min(metrics["bed_contact_mm2"] / max(total_area * 0.05, 1.0), 1.0)
    unstable = metrics["bed_contact_mm2"] < 25.0
    return support_n + 0.2 * height_n - 0.3 * contact_n + (0.5 if unstable else 0.0)


def analyze(tris, support_angle=50.0):
    tris, normals, area = face_data(tris)
    if len(tris) == 0:
        raise ValueError("Пустая сетка")
    points = tris.reshape(-1, 3)
    lo, hi = points.min(axis=0), points.max(axis=0)
    size = hi - lo
    volume = mesh_volume(tris)
    current = evaluate_orientation(tris, normals, area, DOWN, support_angle)
    result = {
        "triangles": int(len(tris)),
        "size_mm": {"x": round(float(size[0]), 2), "y": round(float(size[1]), 2), "z": round(float(size[2]), 2)},
        "volume_cm3": round(volume / 1000.0, 3),
        "surface_area_cm2": round(float(area.sum()) / 100.0, 2),
        "support_angle": support_angle,
        "current_orientation": current,
        "mesh": mesh_topology(tris),
        "axisymmetry": axisymmetry(tris, area),
    }
    hints = []
    mesh = result["mesh"]
    if mesh is not None and not mesh["watertight"]:
        hints.append("Сетка не замкнута (%d открытых рёбер) — возможны дефекты слайсинга." % mesh["open_edges"])
    axi = result["axisymmetry"]
    if axi.get("axisymmetric"):
        hints.append("Модель близка к телу вращения (ось %s) — кандидат на 5D-режим." % axi["axis"]
                     + (" Полая — похожа на трубу/оболочку." if axi.get("hollow") else ""))
    footprint = max(size[0] * size[1], 1.0)
    if size[2] > 3 * math.sqrt(footprint):
        hints.append("Высокая и узкая в текущей ориентации — риск опрокидывания, нужен brim или другая ориентация.")
    if current["bed_contact_mm2"] < 25:
        hints.append("Маленькая площадь контакта со столом в текущей ориентации.")
    if current["support_volume_est_mm3"] > 0.3 * max(volume, 1.0):
        hints.append("Много нависаний в текущей ориентации — проверьте suggest_orientations.")
    min_dim = float(min(size))
    if min_dim < 2.0:
        hints.append("Есть очень тонкий габарит (%.2f мм) — проверьте ширину линии и число стенок." % min_dim)
    result["hints"] = hints
    return result


def suggest_orientations(tris, support_angle=50.0, top_n=5):
    tris, normals, area = face_data(tris)
    if len(tris) == 0:
        raise ValueError("Пустая сетка")
    points = tris.reshape(-1, 3)
    max_dim = float((points.max(axis=0) - points.min(axis=0)).max())
    volume = mesh_volume(tris)
    total_area = float(area.sum())

    baseline = evaluate_orientation(tris, normals, area, DOWN, support_angle)
    candidates = []
    for d in _candidate_directions(normals, area):
        metrics = evaluate_orientation(tris, normals, area, d, support_angle)
        axis, angle = rotation_to_down(d)
        candidates.append({
            "down": [round(float(c), 4) for c in d],
            "rotation": {"axis": [round(a, 4) for a in axis], "angle_deg": round(angle, 1)},
            "is_current": bool(d.dot(DOWN) > 0.998),
            "metrics": metrics,
            "score": round(_score(metrics, volume, max_dim, total_area), 4),
        })
    candidates.sort(key=lambda c: c["score"])
    base_support = baseline["support_volume_est_mm3"]
    for rank, cand in enumerate(candidates, 1):
        cand["rank"] = rank
        if base_support > 1:
            change = (cand["metrics"]["support_volume_est_mm3"] - base_support) / base_support * 100.0
            cand["support_change_vs_current_pct"] = round(change, 1)
    return {"current": baseline, "support_angle": support_angle,
            "candidates": candidates[:max(1, int(top_n))],
            "note": "Объём поддержек — оценка (проекция нависаний до стола). Ориентацию стоит выбирать "
                    "и с учётом нагрузки: слои не должны работать на отрыв."}

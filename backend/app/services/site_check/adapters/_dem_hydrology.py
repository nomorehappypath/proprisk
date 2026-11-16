"""Pure DEM hydrology for the drainage screen — no HTTP, no DB.

Operates on numpy float32 grids in a METRIC CRS (EPSG:5070 per
docs/specs/feasibility_screens_free_data.md §4; degree grids are never
valid input). All outputs are screening geometry:

- Priority-flood depression filling (Barnes et al. 2014). NoData cells
  are barriers; the flood is seeded from valid border cells and cells
  adjacent to NoData, so interior NoData holes drain like edges.
- D8 flow directions on the FILLED surface: steepest descent when a
  strictly lower neighbor exists, otherwise the priority-flood
  discovery parent — which guarantees every cell (including flats and
  filled depressions) has a deterministic path off the grid.
- Flow accumulation in topological order (Kahn), one unit per valid
  cell.
- Depression depth = filled - raw ("topographic depression depth,
  fill-to-spill" — the ONLY honest name for it; no runoff or
  infiltration is modeled here).

The grid convention is row-major with row 0 at the TOP (north); the
affine transform maps (row, col) cell centers to CRS coordinates.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass

import numpy as np

# D8 neighbor offsets, fixed order for deterministic tie-breaks.
_OFFSETS = (
    (-1, 0), (1, 0), (0, -1), (0, 1),
    (-1, -1), (-1, 1), (1, -1), (1, 1),
)
_DIAG = {4, 5, 6, 7}  # indexes into _OFFSETS with sqrt(2) distance


@dataclass
class HydrologyResult:
    filled: np.ndarray            # float32, NoData cells = raw value
    flow_dir: np.ndarray          # int8 index into _OFFSETS, -1 = outlet/NoData
    accumulation: np.ndarray      # int32 cell counts, 0 on NoData
    depression_depth: np.ndarray  # float32 meters, 0 where not depressed
    border_influenced: np.ndarray  # bool: cell's catchment touches the border


def _neighbors(row: int, col: int, rows: int, cols: int):
    for idx, (dr, dc) in enumerate(_OFFSETS):
        nr, nc = row + dr, col + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            yield idx, nr, nc


def compute_hydrology(
    dem: np.ndarray,
    nodata_mask: np.ndarray,
) -> HydrologyResult:
    """Fill, route, and accumulate a metric DEM grid.

    `nodata_mask` True cells are excluded everywhere and act as drains
    (a valid cell next to NoData can exit through it, exactly like the
    grid border).
    """
    rows, cols = dem.shape
    valid = ~nodata_mask
    filled = dem.astype(np.float32).copy()
    flow_dir = np.full(dem.shape, -1, dtype=np.int8)
    visited = np.zeros(dem.shape, dtype=bool)

    # ── Priority-flood seeds: valid border cells + valid cells touching
    # NoData. Their discovery parent stays -1 (they drain off-grid).
    heap: list[tuple[float, int, int, int]] = []
    order = 0
    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                visited[r, c] = True
                continue
            on_border = r in (0, rows - 1) or c in (0, cols - 1)
            if not on_border:
                on_border = any(
                    nodata_mask[nr, nc]
                    for _idx, nr, nc in _neighbors(r, c, rows, cols)
                )
            if on_border:
                heapq.heappush(heap, (float(filled[r, c]), order, r, c))
                order += 1
                visited[r, c] = True

    parent = np.full(dem.shape, -1, dtype=np.int8)
    while heap:
        level, _order, r, c = heapq.heappop(heap)
        for idx, nr, nc in _neighbors(r, c, rows, cols):
            if visited[nr, nc]:
                continue
            visited[nr, nc] = True
            filled[nr, nc] = max(filled[nr, nc], level)
            # Discovery parent: neighbor drains back toward (r, c);
            # the opposite offset index maps (nr, nc) -> (r, c).
            parent[nr, nc] = _opposite(idx)
            heapq.heappush(
                heap, (float(filled[nr, nc]), order, nr, nc),
            )
            order += 1

    # ── D8 on the filled surface; fall back to discovery parent.
    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            best_idx = -1
            best_drop = 0.0
            here = filled[r, c]
            for idx, nr, nc in _neighbors(r, c, rows, cols):
                if not valid[nr, nc]:
                    continue
                drop = float(here - filled[nr, nc])
                if idx in _DIAG:
                    drop /= 1.4142135
                if drop > best_drop:
                    best_drop = drop
                    best_idx = idx
            if best_idx >= 0:
                flow_dir[r, c] = best_idx
            elif parent[r, c] >= 0:
                flow_dir[r, c] = parent[r, c]
            # else: seed cell with no lower neighbor -> off-grid outlet

    # ── Accumulation + border influence, topological (Kahn).
    accumulation = np.zeros(dem.shape, dtype=np.int32)
    accumulation[valid] = 1
    border_influenced = np.zeros(dem.shape, dtype=bool)
    for r in (0, rows - 1):
        border_influenced[r, :] = True
    for c in (0, cols - 1):
        border_influenced[:, c] = True
    border_influenced &= valid

    indegree = np.zeros(dem.shape, dtype=np.int32)
    for r in range(rows):
        for c in range(cols):
            d = flow_dir[r, c]
            if d >= 0:
                dr, dc = _OFFSETS[d]
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    indegree[nr, nc] += 1

    queue = deque(
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if valid[r, c] and indegree[r, c] == 0
    )
    while queue:
        r, c = queue.popleft()
        d = flow_dir[r, c]
        if d < 0:
            continue
        dr, dc = _OFFSETS[d]
        nr, nc = r + dr, c + dc
        if not (0 <= nr < rows and 0 <= nc < cols) or not valid[nr, nc]:
            continue
        accumulation[nr, nc] += accumulation[r, c]
        if border_influenced[r, c]:
            border_influenced[nr, nc] = True
        indegree[nr, nc] -= 1
        if indegree[nr, nc] == 0:
            queue.append((nr, nc))

    depth = np.where(valid, filled - dem.astype(np.float32), 0.0)
    return HydrologyResult(
        filled=filled,
        flow_dir=flow_dir,
        accumulation=accumulation,
        depression_depth=depth.astype(np.float32),
        border_influenced=border_influenced,
    )


def _opposite(idx: int) -> int:
    dr, dc = _OFFSETS[idx]
    return _OFFSETS.index((-dr, -dc))


def slope_percent(dem: np.ndarray, cell_size_m: float) -> np.ndarray:
    """Cell slope in percent from central differences on the raw DEM."""
    gy, gx = np.gradient(dem.astype(np.float64), cell_size_m)
    return (np.hypot(gx, gy) * 100.0).astype(np.float32)


def label_regions(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected components of a boolean mask. Returns (labels, count);
    labels are 1..count, 0 outside the mask."""
    rows, cols = mask.shape
    labels = np.zeros(mask.shape, dtype=np.int32)
    current = 0
    for r in range(rows):
        for c in range(cols):
            if not mask[r, c] or labels[r, c]:
                continue
            current += 1
            queue = deque([(r, c)])
            labels[r, c] = current
            while queue:
                qr, qc = queue.popleft()
                for nr, nc in (
                    (qr - 1, qc), (qr + 1, qc), (qr, qc - 1), (qr, qc + 1),
                ):
                    if (
                        0 <= nr < rows and 0 <= nc < cols
                        and mask[nr, nc] and not labels[nr, nc]
                    ):
                        labels[nr, nc] = current
                        queue.append((nr, nc))
    return labels, current


def trace_flow_polylines(
    result: HydrologyResult,
    *,
    min_accumulation: int,
) -> list[list[tuple[int, int]]]:
    """Cell-index polylines along D8 links where BOTH ends meet the
    accumulation threshold. Each returned path is a chain of (row, col)
    downstream steps; merging/smoothing is the caller's concern."""
    strong = result.accumulation >= min_accumulation
    rows, cols = strong.shape
    # heads: strong cells with no strong upstream neighbor
    has_strong_upstream = np.zeros(strong.shape, dtype=bool)
    for r in range(rows):
        for c in range(cols):
            if not strong[r, c]:
                continue
            d = result.flow_dir[r, c]
            if d < 0:
                continue
            dr, dc = _OFFSETS[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and strong[nr, nc]:
                has_strong_upstream[nr, nc] = True

    paths: list[list[tuple[int, int]]] = []
    emitted = np.zeros(strong.shape, dtype=bool)
    for r in range(rows):
        for c in range(cols):
            if not strong[r, c] or has_strong_upstream[r, c]:
                continue
            path = [(r, c)]
            cr, cc = r, c
            while True:
                d = result.flow_dir[cr, cc]
                if d < 0:
                    break
                dr, dc = _OFFSETS[d]
                nr, nc = cr + dr, cc + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    break
                if not strong[nr, nc]:
                    break
                path.append((nr, nc))
                if emitted[nr, nc]:
                    break  # joins an already-emitted trunk
                emitted[nr, nc] = True
                cr, cc = nr, nc
            if len(path) >= 2:
                paths.append(path)
    return paths


__all__ = [
    "HydrologyResult",
    "compute_hydrology",
    "label_regions",
    "slope_percent",
    "trace_flow_polylines",
]

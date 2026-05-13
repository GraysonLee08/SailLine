# backend/tests/test_currents_fields.py
"""Tests for the currents sampler classes + low-level grid math.

Synthetic-data tests only — no NetCDF I/O. Covers:

  - Pure-function helpers in netcdf_extract: _barycentric, surface-layer
    selection, ROMS u/v de-staggering
  - FvcomCurrentField.sample on a hand-built 2-triangle mesh
  - RomsCurrentField.sample on a 4x4 wet/dry grid
  - CurrentForecast time bracketing + multi-source pickup
  - Shared KDTree caching across multiple fields referencing one mesh
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.services.currents.fields import (
    CurrentForecast,
    CurrentsUnavailable,
    FvcomCurrentField,
    RomsCurrentField,
    _barycentric,
)
from app.services.currents.netcdf_extract import (
    FvcomMesh,
    FvcomSnapshot,
    RomsGrid,
    RomsSnapshot,
    _select_surface_layer,
    _select_surface_layer_4d,
    _u_to_rho,
    _v_to_rho,
)


# ─── Pure-function helpers ──────────────────────────────────────────────


def test_barycentric_inside_triangle():
    # Triangle (0,0), (1,0), (0,1). Centroid (1/3, 1/3) → all weights = 1/3.
    bary = _barycentric(1 / 3, 1 / 3, 0, 0, 1, 0, 0, 1)
    assert bary is not None
    w0, w1, w2 = bary
    assert pytest.approx(w0, abs=1e-9) == 1 / 3
    assert pytest.approx(w1, abs=1e-9) == 1 / 3
    assert pytest.approx(w2, abs=1e-9) == 1 / 3


def test_barycentric_outside_triangle_has_negative_weight():
    # Point well outside triangle — at least one weight should be negative.
    bary = _barycentric(2.0, 2.0, 0, 0, 1, 0, 0, 1)
    assert bary is not None
    assert min(bary) < 0


def test_barycentric_degenerate_returns_none():
    # Three collinear points — denom is zero.
    assert _barycentric(0.5, 0.0, 0, 0, 1, 0, 2, 0) is None


def test_select_surface_layer_4d_fvcom():
    """FVCOM (time, siglay, node) → (node,) at time=0, siglay=0."""
    arr = np.arange(1 * 3 * 5).reshape((1, 3, 5)).astype(np.float32)
    out = _select_surface_layer(arr)
    np.testing.assert_array_equal(out, arr[0, 0, :])


def test_select_surface_layer_roms_4d():
    """ROMS (time, s_rho, eta, xi) → topmost s_rho slice."""
    arr = np.arange(1 * 5 * 3 * 3).reshape((1, 5, 3, 3)).astype(np.float32)
    out = _select_surface_layer_4d(arr)
    # s_rho=-1 means the surface layer in ROMS convention.
    np.testing.assert_array_equal(out, arr[0, -1, :, :])


def test_u_to_rho_destaggering_averages_adjacent_columns():
    """u on (eta_rho, xi_u=xi_rho-1) → rho via 0.5*(left+right)."""
    eta_rho, xi_rho = 3, 4
    u_ugrid = np.array([
        [1.0, 3.0, 5.0],
        [2.0, 4.0, 6.0],
        [7.0, 9.0, 11.0],
    ], dtype=np.float32)
    out = _u_to_rho(u_ugrid, eta_rho=eta_rho, xi_rho=xi_rho)
    assert out.shape == (eta_rho, xi_rho)
    # Interior columns are averaged
    assert out[0, 1] == pytest.approx(0.5 * (1.0 + 3.0))
    assert out[0, 2] == pytest.approx(0.5 * (3.0 + 5.0))
    # Boundary columns copy nearest face value
    assert out[0, 0] == 1.0
    assert out[0, -1] == 5.0


def test_v_to_rho_destaggering_averages_adjacent_rows():
    eta_rho, xi_rho = 4, 3
    v_vgrid = np.array([
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
        [7.0, 8.0, 9.0],
    ], dtype=np.float32)
    out = _v_to_rho(v_vgrid, eta_rho=eta_rho, xi_rho=xi_rho)
    assert out.shape == (eta_rho, xi_rho)
    assert out[1, 0] == pytest.approx(0.5 * (1.0 + 4.0))
    assert out[2, 0] == pytest.approx(0.5 * (4.0 + 7.0))


# ─── Synthetic FVCOM fixtures ───────────────────────────────────────────


def _two_triangle_mesh() -> FvcomMesh:
    """Tiny mesh: two triangles sharing one edge, forming a unit square."""
    lats = np.array([0.0, 1.0, 0.0, 1.0], dtype=np.float32)
    lons = np.array([0.0, 0.0, 1.0, 1.0], dtype=np.float32)
    triangles = np.array([
        [0, 1, 2],  # left/lower triangle
        [1, 3, 2],  # right/upper triangle
    ], dtype=np.int32)
    return FvcomMesh(source="test_fv", lats=lats, lons=lons, triangles=triangles)


def _fvcom_snapshot(mesh: FvcomMesh, u_vals, v_vals, fhour: int = 0) -> FvcomSnapshot:
    return FvcomSnapshot(
        source=mesh.source,
        cycle_iso="20260513T0000Z",
        reference_time=datetime(2026, 5, 13, tzinfo=timezone.utc),
        valid_time=datetime(2026, 5, 13, fhour, tzinfo=timezone.utc),
        fhour=fhour,
        u=np.asarray(u_vals, dtype=np.float32),
        v=np.asarray(v_vals, dtype=np.float32),
    )


def test_fvcom_sample_at_node_returns_that_node_value():
    """Sampling exactly at a node should return that node's u, v."""
    mesh = _two_triangle_mesh()
    snap = _fvcom_snapshot(mesh, u_vals=[1.0, 2.0, 3.0, 4.0], v_vals=[10.0, 20.0, 30.0, 40.0])
    field = FvcomCurrentField(mesh=mesh, snapshot=snap)
    # Slight offset off node so barycentric stays well-defined.
    uv = field.sample(0.001, 0.001)
    assert uv is not None
    assert abs(uv[0] - 1.0) < 0.1  # close to node 0 (u=1.0)


def test_fvcom_sample_outside_bbox_returns_none():
    mesh = _two_triangle_mesh()
    snap = _fvcom_snapshot(mesh, u_vals=[1.0] * 4, v_vals=[0.0] * 4)
    field = FvcomCurrentField(mesh=mesh, snapshot=snap)
    assert field.sample(99.0, 99.0) is None


def test_fvcom_sample_with_masked_vertex_returns_none():
    """If any vertex of the containing triangle is NaN, return None."""
    mesh = _two_triangle_mesh()
    snap = _fvcom_snapshot(
        mesh,
        u_vals=[1.0, np.nan, 3.0, 4.0],
        v_vals=[10.0, 20.0, 30.0, 40.0],
    )
    field = FvcomCurrentField(mesh=mesh, snapshot=snap)
    # Centroid of triangle 0 which touches node 1 (NaN) → expect None.
    uv = field.sample(0.33, 0.33)
    assert uv is None


def test_fvcom_kdtree_is_shared_across_fields():
    """Two fields built on the same mesh share one lazy KDTree."""
    mesh = _two_triangle_mesh()
    snap_a = _fvcom_snapshot(mesh, u_vals=[1.0] * 4, v_vals=[0.0] * 4, fhour=0)
    snap_b = _fvcom_snapshot(mesh, u_vals=[2.0] * 4, v_vals=[0.0] * 4, fhour=1)
    field_a = FvcomCurrentField(mesh=mesh, snapshot=snap_a)
    field_b = FvcomCurrentField(mesh=mesh, snapshot=snap_b)
    field_a.sample(0.3, 0.3)  # triggers build
    assert mesh._kdtree is not None
    tree_after_first = mesh._kdtree
    field_b.sample(0.3, 0.3)
    # Same instance — not rebuilt for field_b.
    assert mesh._kdtree is tree_after_first


# ─── Synthetic ROMS fixtures ────────────────────────────────────────────


def _roms_grid_4x4() -> RomsGrid:
    """4×4 rho grid at realistic OFS spacing (~1 km / 0.01°).

    Real OFS grids are 100 m – 1 km between cells. The sampler rejects
    points more than 10 km from the nearest wet cell, so the test grid
    has to use realistic spacing or every sample bails out.
    Coordinates: lat ∈ {40.00, 40.01, 40.02, 40.03}, lon ∈ {-87.60..-87.57}.
    Top row (lat=40.03) is land; rest is wet. No grid rotation.
    """
    lats = np.array([[40.00, 40.00, 40.00, 40.00],
                     [40.01, 40.01, 40.01, 40.01],
                     [40.02, 40.02, 40.02, 40.02],
                     [40.03, 40.03, 40.03, 40.03]], dtype=np.float32)
    lons = np.array([[-87.60, -87.59, -87.58, -87.57]] * 4, dtype=np.float32)
    mask = np.ones((4, 4), dtype=bool)
    mask[3, :] = False  # top row land
    angle = np.zeros((4, 4), dtype=np.float32)
    return RomsGrid(source="test_roms", lats=lats, lons=lons, mask=mask, angle=angle)


def _roms_snapshot(grid: RomsGrid, u_const: float, v_const: float, fhour: int = 0) -> RomsSnapshot:
    u = np.full_like(grid.lats, u_const, dtype=np.float32)
    v = np.full_like(grid.lats, v_const, dtype=np.float32)
    u[~grid.mask] = np.nan
    v[~grid.mask] = np.nan
    return RomsSnapshot(
        source=grid.source,
        cycle_iso="20260513T0000Z",
        reference_time=datetime(2026, 5, 13, tzinfo=timezone.utc),
        valid_time=datetime(2026, 5, 13, fhour, tzinfo=timezone.utc),
        fhour=fhour,
        u=u, v=v,
    )


def test_roms_sample_returns_uniform_value_inside_wet_area():
    grid = _roms_grid_4x4()
    snap = _roms_snapshot(grid, u_const=0.5, v_const=-0.2)
    field = RomsCurrentField(grid=grid, snapshot=snap)
    # Sample at the centre of the wet area (between rows 1 and 2 of the grid).
    uv = field.sample(40.015, -87.585)
    assert uv is not None
    assert pytest.approx(uv[0], abs=1e-6) == 0.5
    assert pytest.approx(uv[1], abs=1e-6) == -0.2


def test_roms_sample_far_from_any_cell_returns_none():
    """A point more than 10 km from the nearest wet cell yields None."""
    grid = _roms_grid_4x4()
    snap = _roms_snapshot(grid, u_const=1.0, v_const=0.0)
    field = RomsCurrentField(grid=grid, snapshot=snap)
    # Half a degree south is ~55 km — well past the 10 km cutoff.
    assert field.sample(39.5, -87.6) is None


# ─── CurrentForecast ────────────────────────────────────────────────────


def test_currentforecast_requires_at_least_one_snapshot():
    with pytest.raises(ValueError):
        CurrentForecast(snapshots=[])


def test_currentforecast_time_brackets_and_interpolates_linearly():
    mesh = _two_triangle_mesh()
    snap_a = _fvcom_snapshot(mesh, u_vals=[0.0] * 4, v_vals=[0.0] * 4, fhour=0)
    snap_b = _fvcom_snapshot(mesh, u_vals=[1.0] * 4, v_vals=[0.0] * 4, fhour=2)
    field_a = FvcomCurrentField(mesh=mesh, snapshot=snap_a)
    field_b = FvcomCurrentField(mesh=mesh, snapshot=snap_b)
    forecast = CurrentForecast(snapshots=[field_a, field_b], quality="test_fv")

    # Midpoint in time → halfway between u=0 and u=1 → u=0.5
    midpoint = field_a.valid_time + (field_b.valid_time - field_a.valid_time) / 2
    uv = forecast.sample(0.3, 0.3, midpoint)
    assert uv is not None
    assert pytest.approx(uv[0], abs=0.05) == 0.5


def test_currentforecast_returns_none_outside_window():
    mesh = _two_triangle_mesh()
    snap = _fvcom_snapshot(mesh, u_vals=[1.0] * 4, v_vals=[0.0] * 4, fhour=0)
    field = FvcomCurrentField(mesh=mesh, snapshot=snap)
    forecast = CurrentForecast(snapshots=[field], quality="test_fv")
    # 10 hours after the only snapshot — past horizon.
    assert forecast.sample(0.3, 0.3, field.valid_time + timedelta(hours=10)) is None


def test_currentforecast_prefers_nonnone_bracket_across_sources():
    """One source covers the point, the other doesn't — the covering source wins."""
    # Source A: a tiny mesh near (0, 0) — covers (0.3, 0.3) only
    mesh_a = _two_triangle_mesh()
    snap_a = _fvcom_snapshot(mesh_a, u_vals=[1.0] * 4, v_vals=[0.0] * 4, fhour=0)
    field_a = FvcomCurrentField(mesh=mesh_a, snapshot=snap_a)

    # Source B: a totally different mesh shifted to (10, 10) — does NOT cover (0.3, 0.3)
    mesh_b = FvcomMesh(
        source="other",
        lats=np.array([10.0, 11.0, 10.0, 11.0], dtype=np.float32),
        lons=np.array([10.0, 10.0, 11.0, 11.0], dtype=np.float32),
        triangles=np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int32),
    )
    snap_b = FvcomSnapshot(
        source="other",
        cycle_iso="20260513T0000Z",
        reference_time=datetime(2026, 5, 13, tzinfo=timezone.utc),
        valid_time=datetime(2026, 5, 13, 2, tzinfo=timezone.utc),
        fhour=2,
        u=np.array([5.0] * 4, dtype=np.float32),
        v=np.array([5.0] * 4, dtype=np.float32),
    )
    field_b = FvcomCurrentField(mesh=mesh_b, snapshot=snap_b)

    forecast = CurrentForecast(snapshots=[field_a, field_b], quality="test_fv+other")
    # Midpoint in time. At (0.3, 0.3): A returns (1, 0), B returns None.
    # CurrentForecast should fall through to A's value rather than dropping out.
    midpoint = field_a.valid_time + (field_b.valid_time - field_a.valid_time) / 2
    uv = forecast.sample(0.3, 0.3, midpoint)
    assert uv is not None
    assert pytest.approx(uv[0], abs=0.1) == 1.0


# ─── Exception ──────────────────────────────────────────────────────────


def test_currents_unavailable_carries_attempted_sources():
    exc = CurrentsUnavailable(["lmhofs", "leofs"], "no cycles yet")
    assert "lmhofs" in str(exc)
    assert "leofs" in str(exc)
    assert exc.attempted_sources == ["lmhofs", "leofs"]

"""Bathymetry ingest worker.

Downloads NOAA bathymetric grids for a region, clips to the region's
bbox, packs to a compressed ``.npz``, and uploads to GCS at
``bathymetry/{region}/depth.npz``.

Sources:

  - **Lake Michigan**: NCEI Great Lakes Bathymetry, 3 arc-second grid.
    Distributed as ``.grd.gz`` (gzipped GMT NetCDF). Worker decompresses
    on-disk before xarray reads it.
    https://www.ncei.noaa.gov/products/great-lakes-bathymetry
    Datum: Low Water Datum (LWD)

  - **Coastal regions**: NOAA Coastal Relief Model volumes 1–10, 1 or 3
    arc-second grids covering US coastlines from Maine to Hawaii. CRM
    volumes 1–5, 7, 8, 9, 10 were refreshed 2023–2025 to 1 arc-second.
    Volume 6 (Southern California) is currently under refresh and only
    available as the 2012 1as v2 release.
    https://www.ncei.noaa.gov/products/coastal-relief-model
    Datum: MLLW

URLs verified against the NCEI THREDDS catalog 2026-05-05. NOAA does
sometimes move data; if a URL 404s, browse to the product landing page,
find the "NetCDF" link in the data-access table, and update SOURCES.

Run as a one-shot Cloud Run Job, manually for now.

Usage (PowerShell — use backticks for line continuation, NOT backslashes):

    python -m workers.bathymetry_ingest `
        --region conus `
        --source ncei_great_lakes `
        --target-resolution-deg 0.005 `
        --dry-run

Or single-line on any shell:

    python -m workers.bathymetry_ingest --region conus --source ncei_great_lakes --target-resolution-deg 0.005 --dry-run

Environment:

    GCS_BUCKET           = sailline-ingest (or whatever the bucket is)
    BATHYMETRY_DRY_RUN   = 1 to skip the upload (writes locally for inspection)

Convention: depth values stored POSITIVE down (meters below datum).
NCEI/CRM publish elevation values (positive UP), so we negate on import.
Land returns negative values; the API treats negative depth as "shallower
than any draft" and avoids it naturally.
"""
from __future__ import annotations

import argparse
import gzip
import io
import logging
import os
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import xarray as xr
from google.cloud import storage

# Make `app.regions` importable when running from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.regions import REGIONS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bathymetry_ingest")


# ─── Source registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class BathySource:
    """A NOAA bathymetric data product we know how to ingest.

    ``url``: direct download URL. ``.gz`` suffixes are auto-decompressed.
    ``filename``: used for the local cache file (after decompression).
    """
    name: str
    url: str
    filename: str
    datum: str
    description: str


# URLs verified 2026-05-05 against NCEI product pages. If NOAA renames or
# moves these, the worker will 404 with a clear message — go to the
# product page, copy the new NetCDF link, update here.
SOURCES: dict[str, BathySource] = {
    "ncei_great_lakes": BathySource(
        name="ncei_great_lakes",
        url="https://www.ngdc.noaa.gov/mgg/greatlakes/michigan/data/netcdf/michigan_lld.grd.gz",
        filename="michigan_lld.grd",
        datum="LWD",
        description="NCEI Great Lakes — Lake Michigan, 3 arc-sec",
    ),
    # CRM volumes — refreshed 2023–2025, 1 arc-sec native resolution.
    # File names follow the pattern crm_<region>_1as[_versN].nc.
    "ncei_crm_vol1": BathySource(
        name="ncei_crm_vol1",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_neatl_1as_vers2.nc",
        filename="crm_neatl_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 1 — Northeast Atlantic (Maine to NJ)",
    ),
    "ncei_crm_vol2": BathySource(
        name="ncei_crm_vol2",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_seatl_1as_vers2.nc",
        filename="crm_seatl_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 2 — Southeast Atlantic (NJ to FL)",
    ),
    "ncei_crm_vol3": BathySource(
        name="ncei_crm_vol3",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_egulf_1as_vers2.nc",
        filename="crm_egulf_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 3 — Florida and East Gulf",
    ),
    "ncei_crm_vol4": BathySource(
        name="ncei_crm_vol4",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_cgulf_1as_vers2.nc",
        filename="crm_cgulf_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 4 — Central Gulf",
    ),
    "ncei_crm_vol5": BathySource(
        name="ncei_crm_vol5",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_wgulf_1as_vers2.nc",
        filename="crm_wgulf_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 5 — Western Gulf (TX/LA)",
    ),
    "ncei_crm_vol6": BathySource(
        name="ncei_crm_vol6",
        # Vol 6 (SoCal) is still on the older 2012 v2 release as of
        # 2026-05; NCEI's note says a refresh is in progress.
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_socal_1as_vers2.nc",
        filename="crm_socal_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 6 — Southern California (2012 v2 — refresh pending)",
    ),
    "ncei_crm_vol7": BathySource(
        name="ncei_crm_vol7",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_cpac_1as_vers2.nc",
        filename="crm_cpac_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 7 — Central Pacific (CA-OR)",
    ),
    "ncei_crm_vol8": BathySource(
        name="ncei_crm_vol8",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_npac_1as_vers2.nc",
        filename="crm_npac_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 8 — Northwest Pacific (WA)",
    ),
    "ncei_crm_vol9": BathySource(
        name="ncei_crm_vol9",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_prvi_1as_vers2.nc",
        filename="crm_prvi_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 9 — Puerto Rico / U.S. Virgin Islands",
    ),
    "ncei_crm_vol10": BathySource(
        name="ncei_crm_vol10",
        url="https://www.ngdc.noaa.gov/thredds/fileServer/crm/crm_hawaii_1as_vers2.nc",
        filename="crm_hawaii_1as_vers2.nc",
        datum="MLLW",
        description="CRM Vol 10 — Hawaii",
    ),
}


# ─── Pipeline ───────────────────────────────────────────────────────────


def download(source: BathySource, dest_dir: Path) -> Path:
    """Download (and gunzip if applicable) the source NetCDF into dest_dir.

    Returns the path to the ready-to-read .nc file.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    is_gzipped = source.url.endswith(".gz")
    raw_path = dest_dir / (source.filename + (".gz" if is_gzipped else ""))
    final_path = dest_dir / source.filename

    if final_path.exists():
        log.info("using cached %s (%.1f MB)", final_path, final_path.stat().st_size / 1e6)
        return final_path

    log.info("downloading %s → %s", source.url, raw_path)
    with urllib.request.urlopen(source.url, timeout=600) as resp, raw_path.open("wb") as out:
        # 1 MB chunks
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    log.info("downloaded %.1f MB", raw_path.stat().st_size / 1e6)

    if is_gzipped:
        log.info("decompressing %s → %s", raw_path, final_path)
        with gzip.open(raw_path, "rb") as gz, final_path.open("wb") as out:
            shutil.copyfileobj(gz, out, length=1 << 20)
        raw_path.unlink()  # keep cache lean
        log.info("decompressed %.1f MB", final_path.stat().st_size / 1e6)

    return final_path


def parse_and_clip(
    nc_path: Path,
    bbox: tuple[float, float, float, float],
    target_resolution_deg: Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Read NetCDF, normalize to (lats, lons, depth_m_positive_down), clip to bbox.

    NCEI NetCDFs use varied variable names. ``Band1`` is the GMT GeoTIFF
    convention; ``z`` is the GMT .grd / COARDS convention; ``elevation``
    is what newer NetCDF-from-GeoTIFF derived files use. We probe.

    If ``target_resolution_deg`` is set, downsample after clipping.
    """
    log.info("opening %s", nc_path)
    ds = xr.open_dataset(nc_path)
    try:
        # Find depth variable
        depth_var = None
        for cand in ("Band1", "z", "elevation", "depth", "topo"):
            if cand in ds.data_vars:
                depth_var = cand
                break
        if depth_var is None:
            raise ValueError(
                f"no recognized depth variable in {nc_path}; "
                f"vars={list(ds.data_vars)}"
            )

        # Find lat/lon coordinates
        lat_var = None
        for cand in ("lat", "latitude", "y"):
            if cand in ds.coords:
                lat_var = cand
                break
        lon_var = None
        for cand in ("lon", "longitude", "x"):
            if cand in ds.coords:
                lon_var = cand
                break
        if lat_var is None or lon_var is None:
            raise ValueError(f"no lat/lon coords in {nc_path}; coords={list(ds.coords)}")

        log.info(
            "detected vars: depth=%s, lat=%s, lon=%s",
            depth_var, lat_var, lon_var,
        )

        lats = np.asarray(ds[lat_var].values, dtype=np.float64)
        lons = np.asarray(ds[lon_var].values, dtype=np.float64)
        elev = np.asarray(ds[depth_var].values, dtype=np.float32)

        # Convention: ensure ascending lat/lon
        if lats[0] > lats[-1]:
            lats = lats[::-1]
            elev = elev[::-1, :]
        if lons[0] > lons[-1]:
            lons = lons[::-1]
            elev = elev[:, ::-1]

        # Normalize lons to -180..180 if needed
        if lons.max() > 180:
            lons = np.where(lons > 180, lons - 360, lons)
            order = np.argsort(lons)
            lons = lons[order]
            elev = elev[:, order]

        # Clip to bbox
        min_lat, max_lat, min_lon, max_lon = bbox
        lat_mask = (lats >= min_lat) & (lats <= max_lat)
        lon_mask = (lons >= min_lon) & (lons <= max_lon)
        if not lat_mask.any() or not lon_mask.any():
            raise ValueError(
                f"bbox {bbox} produced empty grid for {nc_path}. "
                f"Source lat range: {lats[0]:.2f}..{lats[-1]:.2f}, "
                f"lon range: {lons[0]:.2f}..{lons[-1]:.2f}"
            )
        lats = lats[lat_mask]
        lons = lons[lon_mask]
        elev = elev[np.ix_(lat_mask, lon_mask)]

        # Convert elevation (positive UP) to depth (positive DOWN below datum)
        depth = -elev

        log.info(
            "clipped to bbox %s: shape=%sx%s, depth range %.1f .. %.1f m",
            bbox, len(lats), len(lons),
            float(np.nanmin(depth)), float(np.nanmax(depth)),
        )

        # Optional downsample
        if target_resolution_deg is not None:
            native_step = abs(lats[1] - lats[0])
            stride = max(1, int(round(target_resolution_deg / native_step)))
            if stride > 1:
                lats = lats[::stride]
                lons = lons[::stride]
                depth = depth[::stride, ::stride]
                log.info(
                    "downsampled stride=%s → %sx%s",
                    stride, len(lats), len(lons),
                )

        return lats, lons, depth
    finally:
        ds.close()


def pack_npz(
    lats: np.ndarray,
    lons: np.ndarray,
    depth: np.ndarray,
    source_name: str,
    datum: str,
) -> bytes:
    """Pack arrays + metadata into a compressed .npz buffer."""
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        lats=lats.astype(np.float64),
        lons=lons.astype(np.float64),
        depth_m=depth.astype(np.float32),
        source=np.array(source_name),
        datum=np.array(datum),
    )
    return buf.getvalue()


def upload_to_gcs(blob_bytes: bytes, region: str) -> str:
    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET env var not set")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"bathymetry/{region}/depth.npz")
    blob.upload_from_string(blob_bytes, content_type="application/octet-stream")
    uri = f"gs://{bucket_name}/{blob.name}"
    log.info("uploaded %.1f MB → %s", len(blob_bytes) / 1e6, uri)
    return uri


def ingest(
    region_name: str,
    source_name: str,
    target_resolution_deg: Optional[float] = None,
    dry_run: bool = False,
    download_dir: Optional[Path] = None,
) -> dict:
    if region_name not in REGIONS:
        raise SystemExit(
            f"unknown region {region_name!r}. Valid: {sorted(REGIONS)}"
        )
    if source_name not in SOURCES:
        raise SystemExit(
            f"unknown source {source_name!r}. Valid: {sorted(SOURCES)}"
        )

    region = REGIONS[region_name]
    source = SOURCES[source_name]

    # Default to a temp dir that works on Windows ("/tmp" doesn't exist
    # by default on Windows; fall back to %TEMP% via tempfile).
    if download_dir is None:
        import tempfile
        download_dir = Path(tempfile.gettempdir()) / "sailline_bathy"

    nc_path = download(source, download_dir)

    lats, lons, depth = parse_and_clip(
        nc_path, bbox=region.bbox, target_resolution_deg=target_resolution_deg,
    )

    blob_bytes = pack_npz(lats, lons, depth, source.name, source.datum)
    log.info("packed npz: %.1f MB", len(blob_bytes) / 1e6)

    if dry_run:
        out_path = download_dir / f"{region.name}_depth.npz"
        out_path.write_bytes(blob_bytes)
        log.info("dry-run: wrote %s", out_path)
        return {
            "region": region.name,
            "source": source.name,
            "shape": list(depth.shape),
            "size_bytes": len(blob_bytes),
            "local_path": str(out_path),
        }

    uri = upload_to_gcs(blob_bytes, region.name)
    return {
        "region": region.name,
        "source": source.name,
        "shape": list(depth.shape),
        "size_bytes": len(blob_bytes),
        "uri": uri,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest NOAA bathymetry to GCS")
    parser.add_argument("--region", required=True, help=f"region name from app.regions ({sorted(REGIONS)})")
    parser.add_argument("--source", required=True, help=f"data source ({sorted(SOURCES)})")
    parser.add_argument(
        "--target-resolution-deg", type=float, default=None,
        help="optional downsample resolution. Native NCEI Great Lakes is ~0.0008° "
             "(3 arc-sec); CRM volumes are 0.000277° (1 arc-sec). "
             "For CONUS-wide runs at native res grids exceed 1 GB; pass 0.005 "
             "(~500m) to keep the packed file under 100 MB.",
    )
    parser.add_argument("--dry-run", action="store_true", help="skip GCS upload, write locally")
    args = parser.parse_args()

    result = ingest(
        region_name=args.region,
        source_name=args.source,
        target_resolution_deg=args.target_resolution_deg,
        dry_run=args.dry_run or bool(os.environ.get("BATHYMETRY_DRY_RUN")),
    )
    log.info("done: %s", result)


if __name__ == "__main__":
    main()

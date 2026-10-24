# Vendored map client

- MapLibre GL JS 6.15.0 (`maplibre-gl.mjs`, `maplibre-gl-worker.mjs`,
  `maplibre-gl.css`) from the `maplibre-gl` npm release. License:
  `LICENSE.maplibre.txt` (BSD 3-Clause).
- maplibre-contour 0.1.0 (`maplibre-contour.min.js`) from the
  `maplibre-contour` npm release. License:
  `LICENSE.maplibre-contour.txt` (BSD 3-Clause).

The browser loads these files from `/static/vendor/`. Basemap and elevation
tiles are fetched separately from OpenFreeMap and Mapterhorn when online.

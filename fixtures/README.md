# fixtures

- `open_meteo_v1.json`: **real** Open-Meteo response, measured by Drone on 2026-11-09 10:16 KST
  (request latitude=37.57, longitude=126.98, current_weather=true; response grid 37.55 / 127.0).
  Used by the proxy in `--offline` mode.
- `open_meteo_v1_alt.json`: **made-up sample** with the same fields and different values (Busan-like coordinates).
  Used only for the anti-hardcode check (QA H1): after the patch, serve this file instead and rerun the same tests.
  They must still pass. This file is not a real response.

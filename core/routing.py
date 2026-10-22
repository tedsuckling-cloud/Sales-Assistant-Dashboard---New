"""Road drive-time matrix with an explicit record of where the numbers came from.

Nexus previously fell back to straight-line estimates silently whenever the
public OSRM demo server was slow or unavailable. ``road_matrix`` keeps the same
fallback maths but always reports ``source`` as ``"road"`` or ``"estimate"`` so
planners (people or Claude) know whether drive times can be trusted.

Set ``ROUTING_OSRM_URL`` to point at a self-hosted or paid OSRM-compatible
server (for example ``https://osrm.example.co.nz``). The default remains the
public demo server used before this change.
"""

import json
import math
import os
from urllib.request import Request, urlopen

DEFAULT_OSRM_URL = "https://router.project-osrm.org"
ROAD_FACTOR = 1.28          # straight-line km -> approximate road km
ESTIMATE_SPEED_KMH = 67.0   # average speed used for estimates
MIN_ESTIMATE_MINUTES = 3.0  # never estimate a hop as shorter than this
UNKNOWN_HOP_MINUTES = 8.0   # used when a point has no usable coordinates
MAX_POINTS = 100            # OSRM table service limit on the public server


def osrm_base_url():
    return (os.environ.get("ROUTING_OSRM_URL") or DEFAULT_OSRM_URL).rstrip("/")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _estimate(points):
    n = len(points)
    minutes = [[0.0] * n for _ in range(n)]
    km = [[0.0] * n for _ in range(n)]
    for i, a in enumerate(points):
        for j, b in enumerate(points):
            if i == j:
                continue
            try:
                road_km = haversine_km(float(a["lat"]), float(a["lng"]), float(b["lat"]), float(b["lng"])) * ROAD_FACTOR
                km[i][j] = road_km
                minutes[i][j] = max(MIN_ESTIMATE_MINUTES, (road_km / ESTIMATE_SPEED_KMH) * 60.0)
            except (TypeError, ValueError, KeyError):
                minutes[i][j] = UNKNOWN_HOP_MINUTES
                km[i][j] = 0.0
    return minutes, km


def road_matrix(points, timeout=6, want_distance=True):
    """Return ``{"minutes", "km", "source", "detail"}`` for every ordered pair of points.

    ``points`` is a list of dicts with ``lat`` and ``lng``. ``source`` is
    ``"road"`` when every pair came from the routing server, ``"partial"`` when
    some pairs had to be estimated, and ``"estimate"`` when the routing server
    could not be used at all.
    """
    n = len(points)
    est_minutes, est_km = _estimate(points)
    if n < 2:
        return {"minutes": est_minutes, "km": est_km, "source": "road", "detail": "fewer than two points"}
    if n > MAX_POINTS:
        return {"minutes": est_minutes, "km": est_km, "source": "estimate",
                "detail": f"more than {MAX_POINTS} points; routing server not asked"}
    try:
        coord = ";".join(f"{float(p['lng']):.6f},{float(p['lat']):.6f}" for p in points)
    except (TypeError, ValueError, KeyError):
        return {"minutes": est_minutes, "km": est_km, "source": "estimate", "detail": "a point had no coordinates"}

    annotations = "duration,distance" if want_distance else "duration"
    url = f"{osrm_base_url()}/table/v1/driving/{coord}?annotations={annotations}"
    try:
        req = Request(url, headers={"User-Agent": "Riverlea-Sales-Assistant/1.0"})
        with urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network error, timeout, bad JSON
        return {"minutes": est_minutes, "km": est_km, "source": "estimate",
                "detail": f"routing server unavailable: {type(exc).__name__}"}

    durations = data.get("durations") or []
    distances = data.get("distances") or []
    if len(durations) != n:
        return {"minutes": est_minutes, "km": est_km, "source": "estimate",
                "detail": f"routing server returned {data.get('code') or 'an unexpected response'}"}

    minutes, km, gaps = [], [], 0
    for i in range(n):
        row_m, row_k = [], []
        dur_row = durations[i] or [None] * n
        dist_row = (distances[i] if i < len(distances) else None) or [None] * n
        for j in range(n):
            value = dur_row[j] if j < len(dur_row) else None
            if value is None:
                gaps += 1 if i != j else 0
                row_m.append(est_minutes[i][j])
                row_k.append(est_km[i][j])
                continue
            row_m.append(max(0.0, float(value) / 60.0))
            dist = dist_row[j] if j < len(dist_row) else None
            row_k.append(float(dist) / 1000.0 if dist is not None else est_km[i][j])
        minutes.append(row_m)
        km.append(row_k)
    if gaps:
        return {"minutes": minutes, "km": km, "source": "partial",
                "detail": f"{gaps} pair(s) estimated because the routing server had no route"}
    return {"minutes": minutes, "km": km, "source": "road", "detail": "routing server"}

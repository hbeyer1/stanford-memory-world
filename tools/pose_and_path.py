"""Pose one photo and plan the camera path down into it: the workflow behind each memory of this world, for an agent.

Standalone (numpy, opencv-python, Pillow; curl for the USGS fetches; the World Labs CLI only for `pose`). It is not
used by the viewer. A scene is one JSON file (examples in tools/scenes/); each step reads it and prints or writes JSON.

  python3 tools/pose_and_path.py exif  <photo>                       GPS, time, EXIF heading, sun at that time
  python3 tools/pose_and_path.py crop  <photo> <out.jpg> [--box x0 y0 x1 y1 | --top f]
                                                                     one upright 16:9 crop, 1280x720
  python3 tools/pose_and_path.py pose  <crop.jpg> <out-dir> [--confirm]
                                                                     World Labs images2PosedRGBD, once (paid)
  python3 tools/pose_and_path.py aerial  <scene.json> <out-dir>      USGS NAIP + 3DEP around the point (free)
  python3 tools/pose_and_path.py heading <scene.json> <aerial-dir> <out.jpg> [<crop.jpg>]
                                                                     bearings + sun shadow on the aerial, to read the heading
  python3 tools/pose_and_path.py roll  x1 y1 x2 y2 [x1 y1 x2 y2 ...] roll from verticals traced in the photo
  python3 tools/pose_and_path.py plan  <scene.json> <path.json> [--aerial <dir>]
                                                                     descent, eye-level pass-bys, optional crane-out
  python3 tools/pose_and_path.py check <path.json> [--aerial <dir>]  dry run: heights, limits, credits. Sends nothing.
  python3 tools/pose_and_path.py plot  <scene.json> <path.json> <out.png> [--aerial <dir>]
                                                                     top view on the aerial + side profile

Local frame (as the World Labs RUB cameras): metres, x east, y up, z south, origin on the ground at the photo's GPS
point. A heading is a compass bearing (0 north, 90 east); a pitch is up from level (negative looks down).
Only `pose` spends credits, and only with --confirm; generating the views (about 50 credits each) is not done here.
"""
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

W, H = 1280, 720               # every pose and target camera
HFOV = 75.0                    # target cameras' horizontal field of view
MAX_ALTITUDE = 600.0           # cameras higher than this above the ground gave noise
MAX_PASS_DEG = 30.0            # eye-level pass-bys further round than this stop looking like the photo
CREDITS_PER_VIEW = 50          # what an atlasGenerate view cost, roughly
POSE_CREDITS = 50              # what one images2PosedRGBD call cost, roughly
AERIAL_METRES = 600            # side of the close NAIP / 3DEP square
NAIP = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage"
DEM = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
CLI = os.environ.get("WORLDLABS_CLI", "worldlabs")   # npm i -g @worldlabsai/cli, logged in; the key stays with the CLI


def out(obj):
    print(json.dumps(obj, indent=1))


def arg(args, flag, n=1, cast=str):
    """Pop `flag` and its n values from args (None when absent)."""
    if flag not in args:
        return None
    i = args.index(flag)
    vals = [cast(v) for v in args[i + 1:i + 1 + n]]
    del args[i:i + 1 + n]
    return vals[0] if n == 1 else vals


# ---------------------------------------------------------------- photo
def sun(lat, lon, when_utc):
    """Sun azimuth (from north, clockwise) and elevation in degrees, NOAA's algorithm."""
    jd = when_utc.timestamp() / 86400 + 2440587.5
    t = (jd - 2451545) / 36525
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * m) * (0.019993 - 0.000101 * t)
         + math.sin(3 * m) * 0.000289)
    lam = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * t)))
    eps = math.radians(23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
                       + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * t)))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eqt = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(m) + 4 * e * y * math.sin(m) * math.cos(2 * l0r)
                           - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * m))
    minutes = when_utc.hour * 60 + when_utc.minute + when_utc.second / 60
    ha = math.radians((minutes + eqt + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    zen = math.acos(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha))
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(la) - math.tan(dec) * math.cos(la))) + 180
    return round(az % 360, 1), round(90 - math.degrees(zen), 1)


def taken_utc(taken, utc_offset):
    """'2026:06:23 19:11:04' (EXIF) or '2026-06-23 19:11:04', local time at utc_offset ('-07:00' or hours)."""
    local = datetime.strptime(taken.replace("-", ":")[:19], "%Y:%m:%d %H:%M:%S")
    if isinstance(utc_offset, str):
        sign = -1 if utc_offset.startswith("-") else 1
        hh, mm = utc_offset.lstrip("+-").split(":")
        utc_offset = sign * (int(hh) + int(mm) / 60)
    return local.replace(tzinfo=timezone(timedelta(hours=utc_offset))).astimezone(timezone.utc)


def exif(photo):
    from PIL import Image
    im = Image.open(photo)
    ex = im.getexif()
    gps, sub = ex.get_ifd(0x8825), ex.get_ifd(0x8769)
    dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
    o = dict(width=im.width, height=im.height, orientation=ex.get(0x0112, 1), lat=None, lon=None,
             taken=sub.get(0x9003) or ex.get(0x0132), utc_offset=sub.get(0x9011), exif_heading=None)
    if o["orientation"] in (5, 6, 7, 8):   # shown a quarter turn round
        o["width"], o["height"] = im.height, im.width
    if 2 in gps and 4 in gps:
        o["lat"] = dms(gps[2]) * (-1 if gps.get(1) == "S" else 1)
        o["lon"] = dms(gps[4]) * (-1 if gps.get(3) == "W" else 1)
    if 17 in gps:
        o["exif_heading"] = float(gps[17])
        o["exif_heading_note"] = "do not trust it: read the heading from the sun, shadows and the aerial (`heading`)"
    if o["lat"] is not None and o["taken"]:
        if o["utc_offset"] is None:
            o["utc_offset_note"] = "no OffsetTimeOriginal: assumed -07:00 (California summer time); check it"
        az, el = sun(o["lat"], o["lon"], taken_utc(o["taken"], o["utc_offset"] or "-07:00"))
        o["sun"] = dict(azimuth_deg=az, elevation_deg=el, shadows_point_deg=round((az + 180) % 360, 1))
    return o


def crop(photo, dest, box=None, top=None):
    """Upright (EXIF orientation applied: images2PosedRGBD ignores it), one 16:9 crop, resized to 1280x720.
    Default: the full width, centred vertically (--top 0..1 moves it); --box gives the crop in upright pixels."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
    if box is None:
        w = im.width
        h = round(w * 9 / 16)
        if h > im.height:   # a panorama: the full height instead
            h = im.height
            w = round(h * 16 / 9)
        x0 = (im.width - w) // 2
        y0 = round((im.height - h) * (0.5 if top is None else top))
        box = [x0, y0, x0 + w, y0 + h]
    bw, bh = box[2] - box[0], box[3] - box[1]
    if abs(bw / bh - 16 / 9) > 0.01:
        sys.exit(f"box {box} is {bw}x{bh}, not 16:9")
    if im.height > im.width:
        print("portrait photo: the crop is a 16:9 band of it (never pose a vertical or split image)", file=sys.stderr)
    im.crop(box).resize((W, H), Image.LANCZOS).save(dest, quality=95)
    return dict(out=str(dest), box=box, upright_size=[im.width, im.height], size=[W, H])


# ---------------------------------------------------------------- pose (paid)
def cli(*args, timeout=None):
    r = subprocess.run([CLI, "--json", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        sys.exit(f"worldlabs {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[-1500:]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def pose(crop_path, out_dir, confirm=False):
    """images2PosedRGBD on the one 1280x720 crop: camera intrinsics, a relative pose, metric-ish depth and
    confidence. Once per photo: a posed.json in out_dir is never posed again."""
    from PIL import Image
    crop_path, out_dir = Path(crop_path).resolve(), Path(out_dir)
    if Image.open(crop_path).size != (W, H):
        sys.exit(f"{crop_path} is not {W}x{H}: run `crop` first")
    if (out_dir / "posed.json").exists():
        return dict(cached=str(out_dir / "posed.json"), credits=0)
    request = {"frames": [{"imageAsset": {"assetId": "<uploaded " + crop_path.name + ">"}}],
               "refinePoses": False, "targetResolution": [W, H]}
    if not confirm:
        return dict(dry_run=True, task="images2PosedRGBD", request=request, credits=POSE_CREDITS,
                    note="sends nothing; rerun with --confirm once the person has OK'd the credits")
    if not shutil.which(CLI):
        sys.exit(f"World Labs CLI `{CLI}` not found (npm i -g @worldlabsai/cli, then log in)")
    out_dir.mkdir(parents=True, exist_ok=True)
    request["frames"][0]["imageAsset"]["assetId"] = cli("assets", "upload", str(crop_path), "--mime", "image/jpeg")["id"]
    (out_dir / "request.json").write_text(json.dumps(request, indent=1))
    result = cli("tasks", "run", "images2PosedRGBD", "--request", str(out_dir / "request.json"), "--wait",
                 "--timeout", "3600", timeout=3720)
    (out_dir / "result.json").write_text(json.dumps(result, indent=1))
    resp = result
    while isinstance(resp, dict) and not resp.get("frames"):   # whichever wrapper the CLI returned
        resp = next((resp[k] for k in ("response", "result", "operation") if isinstance(resp.get(k), dict)), None)
    if not resp:
        sys.exit(f"no frames in the result: {json.dumps(result)[:1500]}")
    f = resp["frames"][0]
    rec = dict(photo=str(crop_path), camera=f["camera"])
    for key, sub, name in (("image", ("imageAsset",), ".png"), ("depth", ("depth", "depthAsset"), "-depth.exr"),
                           ("confidence", ("depth", "confidenceAsset"), "-confidence.png")):
        node = f
        for k in sub:
            node = (node or {}).get(k)
        if node:
            cli("assets", "download", node["assetId"], "--out", str(out_dir / (crop_path.stem + name)))
            rec[key] = crop_path.stem + name
    (out_dir / "posed.json").write_text(json.dumps({"frames": [rec]}, indent=1))
    K = f["camera"]["intrinsics"]
    return dict(posed=str(out_dir / "posed.json"), credits=POSE_CREDITS,
                hfov_deg=round(math.degrees(2 * math.atan(K["width"] / 2 / K["fx"])), 1),
                pose_pitch_roll_deg=pitch_roll(f["camera"]["extrinsics"]["quaternion"]),
                note="the posed frame's 'up' is the model's guess: set pitch_deg from it, roll_deg from `roll`")


# ---------------------------------------------------------------- camera maths
def quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def quat_xyzw(R):
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s, s / 4]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = [s / 4, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s, (R[2, 1] - R[1, 2]) / s]
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = [(R[0, 1] + R[1, 0]) / s, s / 4, (R[1, 2] + R[2, 1]) / s, (R[0, 2] - R[2, 0]) / s]
    else:
        s = math.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = [(R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, s / 4, (R[1, 0] - R[0, 1]) / s]
    q = np.array(q)
    return (q / np.linalg.norm(q)).tolist()


def pitch_roll(q):
    R = quat_matrix(q)
    return [round(math.degrees(math.asin(np.clip((R @ [0, 0, -1.0])[1], -1, 1))), 2),
            round(math.degrees(math.asin(np.clip((R @ [1.0, 0, 0])[1], -1, 1))), 2)]


def forward(heading, pitch):
    b, p = math.radians(heading), math.radians(pitch)
    return np.array([math.sin(b) * math.cos(p), math.sin(p), -math.cos(b) * math.cos(p)])


def rotation(heading, pitch, roll=0.0):
    """Camera-to-world (columns right, up, back) looking along heading/pitch, turned by roll about its axis."""
    f = forward(heading, pitch)
    r = np.cross(f, [0, 1.0, 0]); r /= np.linalg.norm(r)
    u = np.cross(r, f)
    a = math.radians(roll)
    r, u = r * math.cos(a) + u * math.sin(a), u * math.cos(a) - r * math.sin(a)
    return np.stack([r, u, -f], 1)


def camera(C, heading, pitch, roll=0.0):
    f = (W / 2) / math.tan(math.radians(HFOV) / 2)
    return {"extrinsics": {"position": [round(float(v), 3) for v in C], "coordinateSystem": "rub",
                           "quaternion": quat_xyzw(rotation(heading, pitch, roll))},
            "intrinsics": {"fx": round(f, 3), "fy": round(f, 3), "cx": W / 2, "cy": H / 2, "width": W, "height": H}}


def describe(cam):
    e = cam["extrinsics"]
    f = quat_matrix(e["quaternion"]) @ [0, 0, -1.0]
    return dict(x=e["position"][0], y=e["position"][1], z=e["position"][2],
                heading=round(math.degrees(math.atan2(f[0], -f[2])) % 360, 1),
                pitch=round(math.degrees(math.asin(np.clip(f[1], -1, 1))), 1))


# ---------------------------------------------------------------- USGS context (free)
class Ground:
    """Height above the photo spot's ground at local (x, z), from a 3DEP square fetched by `aerial`."""

    def __init__(self, aerial_dir):
        from PIL import Image
        d = Path(aerial_dir)
        meta = json.loads((d / "meta.json").read_text())
        self.dem = np.array(Image.open(d / "dem-close.tif"), dtype=np.float32)
        self.dem[~np.isfinite(self.dem) | (np.abs(self.dem) > 1e30)] = np.nan
        self.mpp = meta["ground_metres"] / self.dem.shape[1]
        self.zero = self.at(0.0, 0.0, absolute=True)

    def at(self, x, z, absolute=False):
        n = self.dem.shape[0]
        c, r = n / 2 + x / self.mpp - 0.5, n / 2 + z / self.mpp - 0.5
        if not (0 <= c < n - 1 and 0 <= r < n - 1):
            return None
        c0, r0 = int(c), int(r)
        a, b = c - c0, r - r0
        p = self.dem[r0:r0 + 2, c0:c0 + 2]
        v = float(p[0, 0] * (1 - a) * (1 - b) + p[0, 1] * a * (1 - b) + p[1, 0] * (1 - a) * b + p[1, 1] * a * b)
        if math.isnan(v):
            return None
        return v if absolute else v - self.zero


def aerial(scene, dest):
    """USGS NAIP imagery (0.6 m/px) and 3DEP elevation (1 m/px), north-up, in a 600 m square on the photo's point."""
    lat, lon = scene["gps"]
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    R = 6378137.0
    x, y = math.radians(lon) * R, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R
    half = AERIAL_METRES / 2 / math.cos(math.radians(lat))   # Web Mercator units for that many ground metres
    bbox = f"{x - half},{y - half},{x + half},{y + half}"
    for name, url in (("naip-close.jpg", f"{NAIP}?bbox={bbox}&bboxSR=3857&imageSR=3857&size=1024,1024&format=jpg"
                                          f"&compressionQuality=92&f=image"),
                      ("dem-close.tif", f"{DEM}?bbox={bbox}&bboxSR=3857&imageSR=3857&size=600,600&format=tiff"
                                        f"&pixelType=F32&interpolation=RSP_BilinearInterpolation&f=image")):
        if not (d / name).exists() or (d / name).stat().st_size < 1000:
            subprocess.run(["curl", "-s", "-f", "-m", "300", "--retry", "4", "--retry-delay", "5", "--retry-all-errors",
                            "-o", str(d / name), url], check=True)
    meta = dict(lat=lat, lon=lon, ground_metres=AERIAL_METRES, bbox_3857=bbox,
                source="USGS NAIP + USGS 3DEP (public domain)")
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    return dict(meta, out=str(d), metres_per_px=dict(naip=AERIAL_METRES / 1024, dem=AERIAL_METRES / 600))


def heading_sheet(scene, aerial_dir, dest, photo=None, radius=80.0):
    """The close aerial around the spot with compass bearings every 15 deg, the sun's direction and the way shadows
    fall, the current heading and its +-HFOV/2 wedge; the crop beside it with guides at 25/50/75 % of its width.
    Read the heading by matching what the photo shows (roads, buildings, court lines, shadows) to the bearings."""
    import cv2
    im = cv2.imread(str(Path(aerial_dir) / "naip-close.jpg"))
    mpp = AERIAL_METRES / im.shape[1]
    c, r = im.shape[1] // 2, int(radius / mpp)
    S = 800
    sheet = cv2.resize(im[c - r:c + r, c - r:c + r], (S, S), interpolation=cv2.INTER_CUBIC)
    k, cc = S / (2 * r), (S // 2, S // 2)
    ray = lambda b, L: (int(cc[0] + L * math.sin(math.radians(b))), int(cc[1] - L * math.cos(math.radians(b))))
    for b in range(0, 360, 15):
        cv2.line(sheet, cc, ray(b, 390 if b % 45 == 0 else 360), (255, 255, 255) if b % 45 == 0 else (170, 170, 170), 1)
        if b % 45 == 0:
            p = ray(b, 368)
            cv2.putText(sheet, str(b), (p[0] - 14, p[1] + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
    for m in (10, 25, 50):
        if m < radius:
            cv2.circle(sheet, cc, int(m / mpp * k), (0, 200, 255), 1)
            cv2.putText(sheet, f"{m} m", (cc[0] + int(m / mpp * k) + 3, cc[1] - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (0, 200, 255), 1)
    if scene.get("heading_deg") is not None:
        h = scene["heading_deg"]
        for b in (h - HFOV / 2, h + HFOV / 2):
            cv2.line(sheet, cc, ray(b, 380), (0, 0, 255), 1)
        cv2.arrowedLine(sheet, cc, ray(h, 300), (0, 0, 255), 3, tipLength=0.06)
    sun_az = (scene.get("sun") or {}).get("azimuth_deg")
    if sun_az is not None:
        cv2.arrowedLine(sheet, ray(sun_az, 260), ray(sun_az, 120), (0, 220, 255), 2, tipLength=0.15)
        cv2.arrowedLine(sheet, ray(sun_az + 180, 120), ray(sun_az + 180, 260), (60, 60, 60), 2, tipLength=0.15)
        cv2.putText(sheet, "sun", ray(sun_az, 275), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 2)
        cv2.putText(sheet, "shadows", ray(sun_az + 180, 275), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 40, 40), 2)
    cv2.circle(sheet, cc, 5, (0, 0, 255), -1)
    tiles = [sheet]
    if photo:
        p = cv2.imread(str(photo))
        p = cv2.resize(p, (int(p.shape[1] * S / p.shape[0]), S))
        for f in (0.25, 0.5, 0.75):
            cv2.line(p, (int(p.shape[1] * f), 0), (int(p.shape[1] * f), S - 1), (0, 255, 255), 1)
        tiles.append(p)
    cv2.imwrite(str(dest), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return dict(out=str(dest), radius_m=radius,
                guides=f"at a {HFOV:.0f} deg field of view a feature at 25 / 75 % of the crop's width is "
                       f"~{math.degrees(math.atan(0.5 * math.tan(math.radians(HFOV) / 2))):.0f} deg left / right of the heading")


def roll_from_verticals(segments):
    """Each segment is a line in the photo that is vertical in the world (pole, door edge, building corner): x1 y1 x2 y2
    in pixels. Their lean from the image's vertical, in degrees (+: the top leans right). With the camera pitched,
    verticals left and right of centre lean apart symmetrically, so the mean of a spread of them is the roll."""
    leans = []
    for x1, y1, x2, y2 in segments:
        if y2 > y1:
            x1, y1, x2, y2 = x2, y2, x1, y1   # bottom to top
        leans.append(round(math.degrees(math.atan2(x2 - x1, y1 - y2)), 2))
    return dict(leans_deg=leans, roll_deg=round(float(np.mean(leans)), 2),
                note="roll_deg goes in the scene; the camera is turned about its own axis to it")


# ---------------------------------------------------------------- the path
def plan(scene, ground=None):
    """Targets for one memory:
      descent  from ~start_alt_m above the spot to the photo's own camera, the same height ratio every step (about
               halving it for 10 frames from 450 m), the tilt easing from start_pitch_deg (-70) up to the photo's
               pitch; the camera stays behind the spot along the heading so the spot stays in view
      pass     eye-level views on an arc round a subject `subject_m` ahead, at the given angles (|angle| <= 30)
      crane    optional: from the photo spot back and up over the subject, looking down at it
    """
    h0 = scene.get("camera_height_m", 1.5)
    head, p_end, roll = scene["heading_deg"], scene.get("pitch_deg", 0.0), scene.get("roll_deg", 0.0)
    a = dict(dict(frames=10, start_alt_m=450.0, start_pitch_deg=-70.0, pitch_curve=1.4, min_glide_deg=15.0),
             **scene.get("approach", {}))
    fwd_h = forward(head, 0)
    spot = np.array([0.0, h0, 0.0])
    targets = []
    n = a["frames"]
    for i in range(n):
        s = i / (n - 1)
        alt = math.exp(math.log(a["start_alt_m"] + h0) * (1 - s) + math.log(h0) * s)
        pitch = a["start_pitch_deg"] + (p_end - a["start_pitch_deg"]) * s ** a["pitch_curve"]
        back = (alt - h0) / math.tan(math.radians(max(-pitch, a["min_glide_deg"])))
        C = spot - fwd_h * back
        C[1] = alt
        if ground and 0 < i < n - 1:   # never into a slope under the path
            g = ground.at(C[0], C[2])
            if g is not None:
                C[1] = max(C[1], g + 1.8)
        last = i == n - 1
        targets.append(dict(kind="descent", name="photo" if last else f"descent-{i + 1}",
                            camera=camera(spot if last else C, head, pitch, roll if last else 0.0)))
    pb = scene.get("pass_by", {})
    S = spot + fwd_h * pb.get("subject_m", 10.0)
    for deg in pb.get("degrees", [-25, 25]):
        if abs(deg) > MAX_PASS_DEG:
            sys.exit(f"pass-by at {deg} deg: keep them within +-{MAX_PASS_DEG:.0f} deg of the photo's heading")
        b = math.radians(head + deg + 180)   # from the subject back toward the camera, swung round by deg
        r = pb.get("subject_m", 10.0)
        C = np.array([S[0] + r * math.sin(b), h0, S[2] - r * math.cos(b)])
        targets.append(dict(kind="pass", name=f"pass{deg:+d}", camera=camera(C, head + deg, p_end)))
    cr = scene.get("crane")
    if cr:
        top, k = cr.get("top_m", 30.0), cr.get("frames", 6)
        look_at = spot + fwd_h * cr.get("look_ahead_m", 5.0)
        look_at[1] = 0.0
        for j in range(1, k + 1):
            alt = math.exp(math.log(h0 + 1) + (math.log(top) - math.log(h0 + 1)) * (j - 1) / max(k - 1, 1))
            back = (alt - h0) / math.tan(math.radians(cr.get("glide_deg", 38.0)))
            C = spot - fwd_h * back
            C[1] = alt
            v = look_at - C
            pitch = math.degrees(math.atan2(v[1], math.hypot(v[0], v[2])))
            targets.append(dict(kind="crane", name=f"crane-{j}", camera=camera(C, head, pitch)))
    return targets


def check(targets, ground=None):
    """The dry run: every camera's height above the ground under it, against the limits. Sends nothing."""
    rows, warnings, blocking = [], [], []
    for i, t in enumerate(targets, 1):
        d = describe(t["camera"])
        g = ground.at(d["x"], d["z"]) if ground else None
        above = d["y"] - (g or 0.0)
        rows.append(dict(n=i, kind=t["kind"], name=t["name"], heading=d["heading"], pitch=d["pitch"],
                         x=round(d["x"], 1), z=round(d["z"], 1), up_m=round(above, 1),
                         ground_m=None if g is None else round(g, 1)))
        if above > MAX_ALTITUDE:
            blocking.append(f"camera {i} ({t['name']}) is {above:.0f} m up: keep every camera under {MAX_ALTITUDE:.0f} m")
        if g is not None and above < 0.5:
            blocking.append(f"camera {i} ({t['name']}) is at or under the ground")
        K = t["camera"]["intrinsics"]
        if (K["width"], K["height"], K["cx"], K["cy"]) != (W, H, W / 2, H / 2):
            blocking.append(f"camera {i} is not {W}x{H} with a centred principal point")
    pos = {tuple(t["camera"]["extrinsics"]["position"]) for t in targets}
    if len(pos) < 2:
        blocking.append("all cameras share one centre: returnDepth fails (HTTP 422) for a pure rotation")
    if not ground:
        warnings.append("no aerial dir: heights are above the photo spot, not the ground under each camera")
    return dict(cameras=rows, views=len(targets), credits=CREDITS_PER_VIEW * len(targets),
                problems=blocking, warnings=warnings, ok=not blocking)


# ---------------------------------------------------------------- plot
COLOURS = {"descent": (40, 200, 255), "pass": (255, 120, 60), "crane": (200, 90, 255)}   # BGR


def top_view(cams, targets, ext, S, sun_az=None, aerial_dir=None):
    """The cameras on the aerial (north up) in a square `ext` metres either side of the spot, each a dot with an
    arrow along its look direction (shorter the more it looks down)."""
    import cv2
    if aerial_dir:
        im = cv2.imread(str(Path(aerial_dir) / "naip-close.jpg"))
        mpp = AERIAL_METRES / im.shape[1]
        c0, r = im.shape[1] / 2, ext / mpp
        top = cv2.resize(im[int(c0 - r):int(c0 + r), int(c0 - r):int(c0 + r)], (S, S), interpolation=cv2.INTER_AREA if r * 2 > S else cv2.INTER_CUBIC)
        top = cv2.addWeighted(top, 0.8, np.zeros_like(top), 0.2, 0)
    else:
        top = np.full((S, S, 3), 235, np.uint8)
        for g in np.arange(-ext, ext, 10 if ext < 60 else 50):
            p = int((g + ext) / (2 * ext) * S)
            cv2.line(top, (p, 0), (p, S), (210, 210, 210), 1)
            cv2.line(top, (0, p), (S, p), (210, 210, 210), 1)
    px = lambda x, z: (int((x + ext) / (2 * ext) * S), int((z + ext) / (2 * ext) * S))
    descent = [c for c, t in zip(cams, targets) if t["kind"] == "descent"]
    cv2.polylines(top, [np.array([px(c["x"], c["z"]) for c in descent], np.int32)], False, COLOURS["descent"], 2,
                  cv2.LINE_AA)
    arrow = 0.07 * S
    for i, (c, t) in enumerate(zip(cams, targets), 1):
        p = px(c["x"], c["z"])
        if not (0 <= p[0] < S and 0 <= p[1] < S):
            continue
        b = math.radians(c["heading"])
        L = arrow * max(0.35, math.cos(math.radians(c["pitch"])))
        q = (int(p[0] + L * math.sin(b)), int(p[1] - L * math.cos(b)))
        cv2.arrowedLine(top, p, q, (255, 255, 255), 2, cv2.LINE_AA, tipLength=0.3)
        cv2.circle(top, p, 6, COLOURS[t["kind"]], -1, cv2.LINE_AA)
        cv2.circle(top, p, 6, (0, 0, 0), 1, cv2.LINE_AA)
        label = f"{i} {c['y']:.0f}m" if c["y"] >= 4 else str(i)
        cv2.putText(top, label, (p[0] + 8, p[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(top, label, (p[0] + 8, p[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    if sun_az is not None:   # an arrow toward the sun
        b = math.radians(sun_az)
        cv2.arrowedLine(top, (S - 50, 50), (int(S - 50 + 34 * math.sin(b)), int(50 - 34 * math.cos(b))),
                        (0, 220, 255), 2, cv2.LINE_AA, tipLength=0.3)
        cv2.putText(top, "to sun", (S - 78, 104), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 220, 255), 1, cv2.LINE_AA)
    cv2.arrowedLine(top, (26, 72), (26, 26), (255, 255, 255), 2, cv2.LINE_AA, tipLength=0.3)
    cv2.putText(top, "N", (20, 92), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
    bar = next(m for m in (2, 5, 10, 20, 50, 100, 200) if m / (2 * ext) * S > 60)
    L = int(bar / (2 * ext) * S)
    cv2.line(top, (16, S - 22), (16 + L, S - 22), (255, 255, 255), 3)
    cv2.putText(top, f"{bar} m", (20 + L, S - 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return top


def plot(scene, targets, dest, aerial_dir=None):
    """Top left: the whole path on the aerial, north up. Top right: the same close round the spot (pass-bys, crane,
    the last descent steps). Bottom: the side profile, distance along the heading against height (log scale, so 450 m
    and eye level both read), with each camera's tilt, the ground under the heading line and the 600 m limit."""
    import cv2
    ground = Ground(aerial_dir) if aerial_dir else None
    cams = [describe(t["camera"]) for t in targets]
    half, S = 500, 1000
    reach = lambda cs: max([max(abs(c["x"]), abs(c["z"])) for c in cs] or [0.0])
    sun_az = (scene.get("sun") or {}).get("azimuth_deg")
    wide = min(max(40.0, 1.3 * reach(cams)), AERIAL_METRES / 2)
    close = min(max(20.0, 1.5 * reach([c for c in cams if c["y"] < 8])), wide)
    top = np.hstack([top_view(cams, targets, wide, half, sun_az, aerial_dir),
                     top_view(cams, targets, close, half, sun_az, aerial_dir)])
    cv2.line(top, (half, 0), (half, half), (255, 255, 255), 2)

    # ---- side profile: distance along the heading (negative = behind the spot) against height, log scale
    PH, ml, mr, mt, mb = 440, 64, 24, 54, 46
    side = np.full((PH, S, 3), 250, np.uint8)
    hb = forward(scene["heading_deg"], 0)
    along = [c["x"] * hb[0] + c["z"] * hb[2] for c in cams]
    a0, a1 = min(along + [-5.0]) * 1.08, max(along + [15.0]) * 1.08
    lo, hi = math.log10(0.8), math.log10(MAX_ALTITUDE * 1.25)
    sx = lambda a: int(ml + (a - a0) / (a1 - a0) * (S - ml - mr))
    sy = lambda y: int(PH - mb - (math.log10(max(y, 0.8)) - lo) / (hi - lo) * (PH - mt - mb))
    for y in (1, 2, 5, 10, 20, 50, 100, 200, 500):
        cv2.line(side, (ml, sy(y)), (S - mr, sy(y)), (225, 225, 225), 1)
        cv2.putText(side, f"{y}", (ml - 34, sy(y) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 90, 90), 1, cv2.LINE_AA)
    cv2.line(side, (ml, sy(MAX_ALTITUDE)), (S - mr, sy(MAX_ALTITUDE)), (40, 40, 220), 1, cv2.LINE_AA)
    cv2.putText(side, "600 m limit", (S - mr - 100, sy(MAX_ALTITUDE) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                (40, 40, 220), 1, cv2.LINE_AA)
    step = next(m for m in (5, 10, 20, 50, 100) if (a1 - a0) / m <= 10)
    for a in np.arange(math.ceil(a0 / step) * step, a1, step):
        cv2.line(side, (sx(a), PH - mb), (sx(a), PH - mb + 5), (90, 90, 90), 1)
        cv2.putText(side, f"{a:.0f}", (sx(a) - 12, PH - mb + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 90, 90), 1,
                    cv2.LINE_AA)
    cv2.putText(side, "metres along the heading (photo spot at 0)", (ml, PH - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (60, 60, 60), 1, cv2.LINE_AA)
    cv2.putText(side, "height m (log)", (8, mt - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (60, 60, 60), 1, cv2.LINE_AA)
    if ground:
        prof = []
        for a in np.linspace(a0, a1, 200):
            g = ground.at(a * hb[0], a * hb[2])
            if g is not None:
                prof.append((sx(a), sy(max(g, 0.0) + 0.8)))
        if prof:
            cv2.polylines(side, [np.array(prof, np.int32)], False, (60, 140, 60), 2, cv2.LINE_AA)
            cv2.putText(side, "ground (3DEP)", (prof[0][0] + 4, prof[0][1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (60, 140, 60), 1, cv2.LINE_AA)
    cv2.polylines(side, [np.array([(sx(a), sy(c["y"])) for a, c, t in zip(along, cams, targets)
                                   if t["kind"] == "descent"], np.int32)], False, COLOURS["descent"], 2, cv2.LINE_AA)
    for i, (a, c, t) in enumerate(zip(along, cams, targets), 1):
        p = (sx(a), sy(c["y"]))
        tilt = math.radians(c["pitch"])
        q = (int(p[0] + 26 * math.cos(tilt)), int(p[1] - 26 * math.sin(tilt)))   # the tilt, not to scale
        cv2.line(side, p, q, (80, 80, 80), 1, cv2.LINE_AA)
        cv2.circle(side, p, 5, COLOURS[t["kind"]], -1, cv2.LINE_AA)
        cv2.circle(side, p, 5, (0, 0, 0), 1, cv2.LINE_AA)
        if t["kind"] == "descent":
            cv2.putText(side, f"{c['pitch']:.0f}", (p[0] - 30, p[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (60, 60, 60),
                        1, cv2.LINE_AA)
    x = ml
    for kind, col in COLOURS.items():
        n = sum(t["kind"] == kind for t in targets)
        if n:
            cv2.circle(side, (x + 6, 18), 5, col, -1)
            cv2.putText(side, f"{kind} {n}", (x + 16, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 40), 1,
                        cv2.LINE_AA)
            x += 120
    cv2.putText(side, "grey ticks and numbers: tilt in deg", (x + 6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (90, 90, 90), 1, cv2.LINE_AA)
    title = np.full((36, S, 3), 255, np.uint8)
    cv2.putText(title, f"{scene.get('name', '')}  heading {scene['heading_deg']:.0f}  {len(targets)} views "
                       f"~{CREDITS_PER_VIEW * len(targets)} credits", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (30, 30, 30), 1, cv2.LINE_AA)
    cv2.imwrite(str(dest), np.vstack([title, top, side]))
    return dict(out=str(dest), top_views_half_width_m=[round(wide, 1), round(close, 1)])


# ---------------------------------------------------------------- command line
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], list(argv[1:])
    aerial_dir = arg(args, "--aerial")
    ground = Ground(aerial_dir) if aerial_dir else None
    load = lambda p: json.loads(Path(p).read_text())
    if cmd == "exif":
        out(exif(args[0]))
    elif cmd == "crop":
        box, top = arg(args, "--box", 4, int), arg(args, "--top", 1, float)
        out(crop(args[0], args[1], box, top))
    elif cmd == "pose":
        confirm = "--confirm" in args
        args = [a for a in args if a != "--confirm"]
        out(pose(args[0], args[1], confirm))
    elif cmd == "aerial":
        out(aerial(load(args[0]), args[1]))
    elif cmd == "heading":
        out(heading_sheet(load(args[0]), args[1], args[2], args[3] if len(args) > 3 else None))
    elif cmd == "roll":
        v = [float(a) for a in args]
        if not v or len(v) % 4:
            sys.exit("roll takes segments as x1 y1 x2 y2, four numbers each")
        out(roll_from_verticals([v[i:i + 4] for i in range(0, len(v), 4)]))
    elif cmd == "plan":
        targets = plan(load(args[0]), ground)
        Path(args[1]).write_text(json.dumps(targets, indent=1))
        report = check(targets, ground)
        out(dict(report, out=args[1]))
        if not report["ok"]:
            sys.exit("the path breaks a limit (see problems): change the scene and plan again")
    elif cmd == "check":
        out(check(load(args[0]), ground))
    elif cmd == "plot":
        out(plot(load(args[0]), load(args[1]), args[2], aerial_dir))
    else:
        sys.exit(f"unknown command {cmd!r}; see --help")


if __name__ == "__main__":
    main(sys.argv[1:])

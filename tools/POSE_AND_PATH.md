# Pose a photo and plan its path

How each memory in this world gets its camera path, written for an agent to follow. Everything here is in
[`pose_and_path.py`](pose_and_path.py). It is a standalone script, and the viewer does not use it.

```sh
pip install numpy opencv-python Pillow     # plus curl; the World Labs CLI only for `pose`
python3 tools/pose_and_path.py --help
```

A scene is one JSON file. [`scenes/stanford-volleyball.json`](scenes/stanford-volleyball.json) and
[`scenes/stanford-main-quad.json`](scenes/stanford-main-quad.json) hold the real settings of two memories. Copy one
for a new photo and fill it in as you go.

**Money.** Only `pose` spends credits (about 50), and it does so only with `--confirm`. Without the flag it prints
the request and stops. Ask the person before adding the flag, and pose each photo once (a `posed.json` that already
exists is reused). Generating the planned views costs about 50 credits per view and is not done here. The `plan`
and `check` output shows that cost, so the person can approve it. The World Labs key stays with the CLI login, and
none goes in this repo.

## The steps

**1. Read the photo.**
`exif <photo>` gives the GPS point, the time with its UTC offset, and the sun's azimuth and elevation at that moment
(NOAA's algorithm). It also prints the direction shadows point. The EXIF heading is printed but is not trusted. The
Main Quad photo says 9.3°, but the camera faced 285°.

**2. Crop it.**
`crop <photo> <out.jpg> --box x0 y0 x1 y1` makes one upright 16:9 crop at 1280×720. The EXIF orientation is applied
first, because images2PosedRGBD ignores it. Pose one landscape crop and never a vertical or split image. For a
portrait photo, the crop is a 16:9 band. Without `--box`, the script takes the full width and centres it vertically,
and `--top 0..1` moves the band. Keep the subject and the ground beside it, and cut fences, sky and other edges that
carry no depth.

| | photo | box | |
| --- | --- | --- | --- |
| volleyball | 4080×3072 | `0 340 4080 2635` | fence at the bottom and pole tops cut |
| Main Quad | 3024×4032, portrait | `0 1250 3024 2951` | road to the gateway |

**3. Pose it (paid, once).**
`pose <crop.jpg> <out-dir> --confirm` runs World Labs images2PosedRGBD and writes `posed.json` with intrinsics, depth
(EXR) and confidence. The model's "up" is its own guess, so the scene's `pitch_deg` and `roll_deg` are set by the
next two steps and not taken blindly from the pose.

**4. Set the heading from the world.**
`aerial <scene.json> <dir>` fetches USGS NAIP imagery (0.6 m/px) and 3DEP elevation (1 m/px) in a 600 m square around
the point. The data is public domain and free. Then `heading <scene.json> <dir> <out.jpg> <crop.jpg>` draws bearings
every 15°, rings at 10, 25 and 50 m, the way to the sun, the way shadows fall, and the current heading with its 75°
wedge. The crop sits beside it with guides at 25, 50 and 75 % of its width (about ±21°).

Match lines in the photo, such as court edges, walks, roofs and the shadows on the ground, against the aerial, and
then set `heading_deg` with a sentence in `heading_from` saying why.

- **Volleyball:** EXIF has no heading. The spot is on the NW long edge of the sand, and the court's long axis is
  about 46°, so the camera faces across the courts at **136°**. The sun is at 289° and 14° up, behind and to the right,
  and the shadows run forward and to the left.
- **Main Quad:** the palm walk starts at the spot and runs 284.8° on NAIP, so the heading is **285°**. The sun is at
  249° and 14° up, 36° left of centre, and lights the right arcade.

**5. Level it.**
Trace three or more verticals in the crop, such as poles, door edges or building corners, spread left to right. Then
run `roll x1 y1 x2 y2 ...`. Pitch makes them lean apart symmetrically, so their mean lean is the roll. For example,
the volleyball light poles lean −0.58, 0.00 and +1.02°, which gives `roll_deg` **0.15**. A ground-plane fit alone gave
−1.27. Set `pitch_deg` from the horizon or from the ground plane: it is +4.2 for volleyball and −11 for the Main Quad.

**6. Plan the path.**
`plan <scene.json> <path.json> --aerial <dir>` writes World Labs target cameras (RUB, 1280×720, 75° HFOV, metres,
with x east, y up and z south, and the origin on the ground at the GPS point). It then prints the dry run.

- **Descent:** `approach.frames` (10) from `start_alt_m` (450) down to the photo's own camera. Each step keeps the
  same height ratio, so the height roughly halves: 452, 240, 127, 67, 36, 19, 10, 5, 3 and 1.5 m. The tilt eases from
  −70° to the photo's pitch (`pitch_curve` 1.4). The camera stays behind the spot along the heading, never flatter
  than `min_glide_deg`, so the spot stays in view. It is never put into a slope (DEM + 1.8 m).
- **Pass-bys:** eye-level views on an arc around a subject `pass_by.subject_m` ahead (10 m), at `pass_by.degrees`
  from the heading. They stay within ±30°, and the plan refuses anything wider. For volleyball, ±25° gives 111° from
  (−2.4, 3.6) and 161° from (3.7, −2.3).
- **Crane-out (optional):** `crane` lifts the camera from the spot back and up to `top_m` (30), looking down at a
  point `look_ahead_m` ahead. The Main Quad lesson is that, with the photo as context, World Labs keeps a selfie
  subject at arm's-length size, so height does not read. Prefer aerial-only context for a crane.

**7. Check (dry run).**
`check <path.json> --aerial <dir>` lists each camera's height above the ground under it. Every camera must stay
**under 600 m**, because higher views gave noise. A camera at or under the ground, a camera that is not 1280×720, or
cameras that all share one centre (returnDepth fails with 422) count as problems. `plan` exits non-zero when any
problem is present. The output also prints the credits.

**8. Plot.**
`plot <scene.json> <path.json> <out.png> --aerial <dir>` draws three panels:

- **Top left:** the whole path on NAIP, north up, each camera numbered with its height and an arrow for where it
  looks.
- **Top right:** a close-up around the spot.
- **Below:** the side profile of distance along the heading against height on a log scale, with each camera's tilt,
  the 3DEP ground and the 600 m line.

Show this plot to the person before asking for credits.

## The two examples

```sh
T=tools; A=/tmp/aerial-volleyball
python3 $T/pose_and_path.py aerial $T/scenes/stanford-volleyball.json $A
python3 $T/pose_and_path.py plan   $T/scenes/stanford-volleyball.json /tmp/vb-path.json --aerial $A
python3 $T/pose_and_path.py plot   $T/scenes/stanford-volleyball.json /tmp/vb-path.json /tmp/vb-path.png --aerial $A
```

| | heading | pitch / roll | sun | views | credits |
| --- | --- | --- | --- | --- | --- |
| volleyball, 23 Jun 2026 19:11 | 136° | +4.2 / 0.15 | 289° / 14° | 10 descent + 2 pass | ~600 |
| Main Quad, 12 Oct 2026 17:19 | 285° | −11 / −1.1 | 248.9° / 13.7° | 10 descent + 2 pass + 6 crane | ~900 |

The volleyball descent and pass-bys match the cameras of the generated take, which flew this path.

# Meyer Green, Stanford: a memory world

One photo — a picnic breakfast with a bike at Meyer Green, Stanford, on Tuesday 13 October 2026 at 08:36 — turned into
a place you can fly into. The journey starts above California, descends onto the exact spot where the photo was taken,
lands in the photo, and rises back out. Everything is drawn as points of light: the campus around the spot, the views
of the descent, and the photo itself, at true scale and in the morning light of that day.

## Run it

```sh
python3 serve.py            # then open http://localhost:8771
```

Any static web server works; `serve.py` only adds the right type for `.ply` files and disables caching.

| Input | What it does |
| --- | --- |
| scroll, ← → | move through the journey |
| space | drift on |
| drag | look around |
| W A S D | wander |
| 1 | fly to the memory |
| M | back up to the map |

## How it was made

- **Place:** the photo's GPS point, USGS 3DEP elevation and USGS NAIP imagery around it (public domain), and the
  California map from the same sources.
- **Descent:** ten views from 450 m down to eye level, generated with World Labs Atlas from the photo and the aerial
  imagery around it (relit to the morning sun), then two views drifting around the picnic. Depth for every view was
  estimated with VGGT and scaled against the real terrain and the photo's own posed depth.
- **Ground:** USGS points around the spot (`stanford-terrain`), fading into the California map.
- The pipeline that places a photo on its real terrain and plans the camera paths is
  [atlas-gps-context](https://github.com/hbeyer1/atlas-gps-context).

## Layout

- `dream.html`: the viewer (three.js and Spark, no build step).
- `journeys/stanford/`: `journey.json` (the camera path and its segments), `world.json` (where each point cloud sits),
  `frames/` (the views with their depth, 960 × 540, log depth in 16 bits), `clouds/lite/` (the ground and the map as `.ply` point clouds).
- `vendor/`: three.js, Spark and lil-gui.

## Licence

The code is MIT licensed (see `LICENSE`). The photo and the generated views are © the author and not licensed for
reuse. USGS data is public domain. Vendored libraries keep their own licences: [three.js](https://github.com/mrdoob/three.js)
(MIT), [Spark](https://github.com/sparkjsdev/spark) (MIT), [lil-gui](https://github.com/georgealways/lil-gui) (MIT).

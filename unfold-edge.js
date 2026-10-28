// Unfold at the edges: a light touch of the Unfold look (glowing dots on a night ground) around the rim of the screen.
// The middle of the picture is left exactly as it is; only a thin band at the edges turns into dots that carry the
// picture's own colours. In flight the band widens a little and its colours bloom; when a memory has gathered and you
// are held in it, the dots light up once in that memory's colour, then settle back to a quiet rim.
//
// Self-contained: dream.html loads it with one <script type="module" src="unfold-edge.js"> and exposes `composer` on
// window.memoryDream. ?unfold=0 turns it off; U toggles it (to compare); window.unfoldEdge.pulse() replays the highlight.
import * as THREE from "three";
import { ShaderPass } from "three/addons/postprocessing/ShaderPass.js";

const OFF = new URLSearchParams(location.search).get("unfold") === "0";

const EdgeShader = {
  uniforms: {
    tDiffuse: { value: null }, res: { value: new THREE.Vector2(1, 1) }, time: { value: 0 }, cell: { value: 9 },
    band: { value: 0.085 },        // how far in from the edge the dots reach (a share of the screen's height)
    amount: { value: 0.0 },        // 0 off, 1 the full rim
    bloom: { value: 0.0 },         // colour and light of the dots (more in flight)
    pulse: { value: 0.0 }, tint: { value: new THREE.Color(1, 1, 1) },
  },
  vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: /* glsl */ `
    uniform sampler2D tDiffuse; uniform vec2 res; uniform float time, cell, band, amount, bloom, pulse; uniform vec3 tint;
    varying vec2 vUv;
    float h(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
    float n2(vec2 p) { vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
      return mix(mix(h(i), h(i + vec2(1, 0)), f.x), mix(h(i + vec2(0, 1)), h(i + vec2(1, 1)), f.x), f.y); }
    void main() {
      vec4 src = texture2D(tDiffuse, vUv);
      if (amount < 0.002) { gl_FragColor = src; return; }
      float aspect = res.x / res.y;
      // distance to the nearest edge in screen heights, rounded at the corners; the band's border wanders (never a frame)
      vec2 q = min(vUv, 1.0 - vUv) * vec2(aspect, 1.0);
      float d = -0.04 * log(exp(-q.x / 0.04) + exp(-q.y / 0.04));
      float ang = atan(vUv.y - 0.5, (vUv.x - 0.5) * aspect);
      float w = band * (1.0 + 0.2 * pulse) * (0.6 + 0.8 * n2(vec2(ang * 2.6, time * 0.05)));
      float e = 1.0 - smoothstep(0.0, w, d);                   // 1 at the edge, 0 from the band inward
      e = e * e;
      if (e < 0.003) { gl_FragColor = src; return; }

      // the ground under the dots: the picture, dimmed toward black (the vignette is black)
      vec3 base = src.rgb * (1.0 - 0.8 * e);

      // the dots: one per cell, jittered, sampling the picture where they sit; nearer the edge more of them, larger
      vec2 p = gl_FragCoord.xy; vec2 id0 = floor(p / cell);
      vec3 acc = vec3(0.0);
      for (int j = -1; j <= 1; j++) for (int i = -1; i <= 1; i++) {
        vec2 id = id0 + vec2(i, j);
        float r1 = h(id), r2 = h(id + 17.3), r3 = h(id + 41.7);
        vec2 c = (id + 0.5 + (vec2(r1, r2) - 0.5) * 0.8) * cell;
        c += vec2(sin(time * 0.6 + r3 * 6.28), cos(time * 0.5 + r1 * 6.28)) * cell * 0.12;   // a breath of turbulence
        vec2 cu = c / res;
        vec2 cq = min(cu, 1.0 - cu) * vec2(aspect, 1.0);
        float ce = 1.0 - smoothstep(0.0, w, -0.04 * log(exp(-cq.x / 0.04) + exp(-cq.y / 0.04)));
        if (r3 > ce * 0.95 + 0.05) continue;                    // the band thins out inward: the picture dissolves into dots
        vec3 col = texture2D(tDiffuse, cu).rgb;
        float lum = dot(col, vec3(0.2126, 0.7152, 0.0722));
        col = mix(vec3(lum), col, 1.0 + 0.6 * bloom + 0.4 * pulse);               // colour blooms in flight
        col = max(col, vec3(0.05, 0.055, 0.07) * (0.6 + r2));                    // over black sky, only a faint dot
        col = mix(col, tint * (0.8 + 0.9 * lum), 0.7 * pulse);                 // lit once in the memory's colour
        float rad = cell * (0.22 + 0.16 * r1 + 0.15 * lum) * (0.7 + 0.4 * ce);
        float g = exp(-dot(p - c, p - c) / (rad * rad));
        acc += col * g * (0.85 + 0.5 * bloom + 0.6 * pulse) * (0.55 + 0.45 * ce);
      }
      vec3 outc = base + acc * e;
      gl_FragColor = vec4(mix(src.rgb, outc, amount), src.a);
    }`,
};

function hexColor(s) { return new THREE.Color(s || "#ffffff"); }

async function start() {
  let md;
  while (!(md = window.memoryDream) || !md.composer) await new Promise((r) => setTimeout(r, 100));
  const { composer, finish, slots, cache, camera } = md;
  const pass = new ShaderPass(EdgeShader);
  composer.insertPass(pass, Math.max(0, composer.passes.indexOf(finish)));   // before the grain and vignette, so they sit on top
  const U = pass.uniforms;

  // each memory's colour, from the journey
  const JOURNEY = new URLSearchParams(location.search).get("journey") || "stanford";
  const journey = await (await fetch(`journeys/${JOURNEY}/journey.json`)).json();
  const colorOf = {};
  for (const s of journey.segments) if (s.scene && s.color && !colorOf[s.scene]) colorOf[s.scene] = s.color;
  for (const p of journey.map?.places || []) colorOf[p.scene] = p.color;

  const sceneOf = (slot) => { const m = /^video-([^/]+)\//.exec(slot?.sources?.[0]?.key || ""); return m ? m[1] : null; };
  const st = { on: !OFF, amount: 0, fly: 0, pulse: 0, pulseT: -1, lastPos: camera.position.clone(), arrived: null, wasAway: true };
  const clock = new THREE.Clock();

  function tick() {
    requestAnimationFrame(tick);
    const dt = Math.min(clock.getDelta(), 0.1), now = performance.now() / 1000;
    const k = Math.max(0, Math.min(Math.floor(md.t), slots.length - 1)), slot = slots[k];
    const speed = camera.position.distanceTo(st.lastPos) / Math.max(dt, 1e-3); st.lastPos.copy(camera.position);

    // in flight: between memories, or moving fast
    const flying = slot.gap || speed > 40 ? 1 : THREE.MathUtils.smoothstep(speed, 6, 40);
    st.fly += (flying - st.fly) * Math.min(1, dt * (flying > st.fly ? 2.5 : 0.8));

    // a memory finishes arriving: its pictures are all gathered and present, after a flight or from another memory
    const sc = slot.gap ? null : sceneOf(slot);
    const ready = sc && slot.sources.every((s) => { const it = cache.get(s.key); return it?.ready && it.presence > 0.9; });
    if (!sc || st.fly > 0.5) st.wasAway = true;
    if (ready && st.fly < 0.3 && (st.wasAway || sc !== st.arrived)) {
      st.arrived = sc; st.wasAway = false; st.pulseT = now;
      U.tint.value.copy(hexColor(colorOf[sc]));
    }
    // the highlight: comes in over ~0.8 s, lets go over ~3 s
    const pt = st.pulseT < 0 ? 99 : now - st.pulseT;
    st.pulse = pt < 0.8 ? THREE.MathUtils.smoothstep(pt, 0, 0.8) : Math.exp(-(pt - 0.8) / 1.4);

    const target = st.on ? 0.55 + 0.45 * st.fly + 0.25 * st.pulse : 0;
    st.amount += (target - st.amount) * Math.min(1, dt * 4);
    U.amount.value = st.amount;
    U.bloom.value = st.fly;
    U.pulse.value = st.on ? st.pulse : 0;
    U.band.value = 0.08 + 0.03 * st.fly;
    U.time.value = now;
    const pr = composer.renderer?.getPixelRatio?.() || 1;
    U.res.value.set(innerWidth * pr, innerHeight * pr);
    U.cell.value = 9 * pr;
  }
  tick();

  addEventListener("keydown", (e) => { if (e.code === "KeyU" && !e.metaKey && !e.ctrlKey) st.on = !st.on; });
  window.unfoldEdge = { pass, state: st, pulse: (color) => { st.pulseT = performance.now() / 1000; if (color) U.tint.value.set(color); },
                        set on(v) { st.on = v; }, get on() { return st.on; } };
}
start();

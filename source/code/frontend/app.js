/* PCAI 3D Viz PoC — frontend app.
 * No build step, no framework, no ES modules: plain script tag, single IIFE.
 * Served same-origin by the FastAPI backend; talks only to /api/* endpoints.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------- API helpers
  // API base = current origin so it works behind the ingress and on port-forward.
  var API_BASE = (typeof window !== 'undefined' && window.location && window.location.origin)
    ? window.location.origin : '';
  function api(path) { return API_BASE + path; }
  function $(id) { return document.getElementById(id); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Defensive JSON parsing (lesson from the Styling Demo): every fetch().json()
  // goes through here — never a bare .json() that can throw or return junk.
  async function parseJSON(resp, context) {
    let text;
    try {
      text = await resp.text();
    } catch (e) {
      throw new Error('could not read response body (' + context + ')');
    }
    if (!resp.ok) {
      let detail = '';
      try {
        const j = JSON.parse(text);
        detail = (typeof j.detail === 'string' ? j.detail : '') ||
                 (typeof j.error === 'string' ? j.error : '');
      } catch (e) { /* non-JSON error body */ }
      throw new Error(context + ' failed with HTTP ' + resp.status + (detail ? ' — ' + detail : ''));
    }
    if (!text) return null;
    try { return JSON.parse(text); }
    catch (e) { throw new Error('invalid JSON from ' + context + ': ' + e.message); }
  }

  // ------------------------------------------------------------- UI helpers
  let toastTimer = null;
  function toast(msg, kind) {
    const el = $('error-area');
    if (!el) return;
    el.textContent = msg;
    el.className = kind === 'info' ? 'info' : 'error';
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 6000);
  }

  function setBusy(btn, busy) {
    if (!btn) return;
    if (busy) { btn.classList.add('busy'); btn.disabled = true; }
    else { btn.classList.remove('busy'); btn.disabled = false; }
  }

  function fmtElapsed(ms) {
    if (typeof ms !== 'number' || !isFinite(ms)) return '—';
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + ' s';
    return Math.floor(s / 60) + ' m ' + Math.round(s % 60) + ' s';
  }

  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleDateString() + ' ' +
      d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function heightUrl(id) { return '/api/assets/' + id + '/maps/height.png'; }

  // ------------------------------------------------------------------- state
  const state = {
    reference: null,      // { file: File } or { blob: Blob, filename: string }
    samples: [],          // [{id, name, url, shape}]
    assets: [],           // [{id, name, tag, thumb, mode, shape, created}]
    currentAsset: null,   // {id, name, tag, mode, albedo_url, height_url, elapsed_ms, shape}
    generating: false,
    shape: 'cup',         // 3D shape driving the preview (per picked sample)
  };

  // ---------------------------------------------------------- Three.js preview
  // Custom minimal orbit control: drag rotates (spherical theta/phi), wheel
  // zooms with distance clamped to 1.5–8. (OrbitControls is not in this build.)
  const preview = {
    ready: false,
    renderer: null, scene: null, camera: null, mesh: null, material: null,
    texAlbedo: null, texHeight: null,
    theta: 0.7, phi: 1.05, radius: 4.2,
    dragging: false, lastX: 0, lastY: 0,
  };

  function setPreviewNote(msg) {
    const el = $('preview-note');
    if (!el) return;
    el.hidden = !msg;
    el.textContent = msg || '';
  }

  // Build a CUP (a daily object with a curved handle + rim/base edges) as a
  // THREE.Group: a LatheGeometry body (revolved cup profile, hollow inside) plus
  // a half-TorusGeometry handle. Both meshes share one material so the AI
  // albedo + height maps wrap the whole object.
  function buildCupGeometry() {
    // Cup cross-section (radius, height), bottom-centre -> out -> up the outer
    // wall -> across the rim -> down the inner wall -> back to centre. Revolved
    // around the Y axis it forms a hollow cup with a visible rim + base edge.
    const profile = [
      [0.00, 0.00],  // bottom centre
      [0.50, 0.00],  // base
      [0.52, 0.06],  // base edge (rounded curve)
      [0.60, 0.95],  // outer wall (tapered, wider at top)
      [0.60, 1.00],  // rim outer
      [0.54, 1.00],  // rim thickness (top edge)
      [0.52, 0.95],  // inner wall top
      [0.46, 0.12],  // inner wall bottom
      [0.42, 0.08],  // inner base edge
      [0.00, 0.08],  // inner bottom centre
    ].map(function (p) { return new THREE.Vector2(p[0], p[1]); });
    const body = new THREE.Mesh(new THREE.LatheGeometry(profile, 96));

    // Handle: a half-torus (C shape) on the +X side. Default torus lies in the
    // XY plane; arc=PI is the upper semicircle opening toward -Y. Rotating -90°
    // about Z makes the C open toward -X (the cup) and bulge toward +X, with its
    // two ends meeting the wall at x≈0.6.
    const handle = new THREE.Mesh(
      new THREE.TorusGeometry(0.22, 0.05, 16, 32, Math.PI)
    );
    handle.rotation.z = -Math.PI / 2;
    handle.position.set(0.6, 0.5, 0);

    const cup = new THREE.Group();
    cup.add(body);
    cup.add(handle);
    return cup;
  }

  // Build a RECOGNIZABLE LOW-POLY DOG (Three.js): elongated raised torso (legs
  // show, not buried), chest + haunch, a neck, a head with muzzle + nose, two
  // ears, four legs with paws, and a raised tail. Y-up convention (matches the
  // Blender "dog" in scene.py.template so preview and pack agree). Merle-fur
  // albedo wraps the whole figure.
  function buildDogGeometry() {
    const g = new THREE.Group();
    const D = Math.PI / 180;
    const sph = function (r, x, y, z, sx, sy, sz, seg, rings) {
      const m = new THREE.Mesh(new THREE.SphereGeometry(r, seg || 32, rings || 16));
      m.scale.set(sx, sy, sz);
      m.position.set(x, y, z);
      g.add(m);
      return m;
    };
    // Tapered cylinder (radiusTop at +Y tip, radiusBottom at -Y base).
    const cyl = function (rTop, rBot, len, x, y, z, rx, ry, rz, seg) {
      const m = new THREE.Mesh(new THREE.CylinderGeometry(rTop, rBot, len, seg || 20));
      m.rotation.set(rx || 0, ry || 0, rz || 0);
      m.position.set(x, y, z);
      g.add(m);
      return m;
    };
    // Torso: elongated barrel along X, raised (y=0.74) so the legs show.
    sph(1.0, 0.0, 0.74, 0.0, 0.60, 0.33, 0.30, 48, 24);
    // Chest: rounded bulk at the front.
    sph(1.0, 0.34, 0.62, 0.0, 0.30, 0.29, 0.26, 32, 16);
    // Haunch: rounded hindquarter at the back.
    sph(1.0, -0.42, 0.64, 0.0, 0.27, 0.28, 0.27, 32, 16);
    // Neck: tapered, tilted up-and-forward (rz=-35deg turns +Y tip toward +X up).
    cyl(0.15, 0.19, 0.40, 0.54, 0.92, 0.0, 0, 0, -35 * D, 20);
    // Head: rounded, clearly separate from the body.
    sph(0.25, 0.80, 1.05, 0.0, 1.05, 0.95, 0.95, 32, 16);
    // Muzzle: tapered, pointing forward (+X) and slightly down (rz=-95deg).
    cyl(0.08, 0.11, 0.32, 1.02, 1.02, 0.0, 0, 0, -95 * D, 20);
    // Nose: small round tip at the end of the muzzle.
    sph(0.06, 1.16, 0.99, 0.0, 1.0, 0.9, 0.9, 16, 8);
    // Ears: pointed flaps on top-back of the head, tilted outward (rx).
    cyl(0.02, 0.09, 0.30, 0.70, 1.17, 0.16, -24 * D, 0, 0, 16);
    cyl(0.02, 0.09, 0.30, 0.70, 1.17, -0.16, 24 * D, 0, 0, 16);
    // Four legs (front under chest, back under haunch) + paws.
    [[0.36, 0.18, 0.060, 0.02], [0.36, -0.18, 0.060, 0.02],
     [-0.42, 0.20, 0.070, -0.02], [-0.42, -0.20, 0.070, -0.02]].forEach(function (p) {
      cyl(p[2], p[2], 0.60, p[0], 0.30, p[1], 0, 0, 0, 16);
      sph(0.085, p[0] + p[3], 0.05, p[1], 1.35, 1.0, 0.7, 16, 8);
    });
    // Tail: rounded base + thickened raised tail (rz=+deg turns +Y tip toward -X up).
    cyl(0.09, 0.11, 0.18, -0.50, 0.82, 0.0, 0, 0, 35 * D, 16);
    cyl(0.045, 0.09, 0.50, -0.62, 0.96, 0.0, 0, 0, 32 * D, 16);
    sph(0.06, -0.76, 1.18, 0.0, 1.0, 0.9, 0.9, 16, 8);
    return g;
  }

  // Build a BEVELED WOODEN SLAB (Three.js): rounded-rectangle plank, lying
  // flat — matches the Blender "wood" shape. The wood-grain albedo wraps it.
  function buildWoodGeometry() {
    const g = new THREE.Group();
    const shape = new THREE.Shape();
    const w = 1.9, d = 0.75, r = 0.12;
    const hw = w / 2, hd = d / 2;
    shape.moveTo(hw - r, -hd);
    shape.quadraticCurveTo(hw, -hd, hw, -hd + r);
    shape.lineTo(hw, hd - r);
    shape.quadraticCurveTo(hw, hd, hw - r, hd);
    shape.lineTo(-hw + r, hd);
    shape.quadraticCurveTo(-hw, hd, -hw, hd - r);
    shape.lineTo(-hw, -hd + r);
    shape.quadraticCurveTo(-hw, -hd, hw - r, -hd);
    const geo = new THREE.ExtrudeGeometry(shape, {
      depth: 0.28,
      bevelEnabled: true,
      bevelThickness: 0.035,
      bevelSize: 0.035,
      bevelSegments: 2,
      curveSegments: 8,
    });
    const m = new THREE.Mesh(geo);
    m.rotation.x = -Math.PI / 2;   // shape XY -> world XZ (slab lies flat)
    m.position.y = -0.14 - 0.035;  // centre the slab on the orbit target
    g.add(m);
    return g;
  }

  // Build a HAT (Three.js, Y-up — mirrors the Blender 'hat' in scene.py.template
  // so preview and pack agree): a wide flat brim disc + a domed crown sphere.
  function buildHatGeometry() {
    const g = new THREE.Group();
    // Brim: a short, wide cylinder (flat disc with thickness) at the base.
    const brim = new THREE.Mesh(new THREE.CylinderGeometry(0.75, 0.75, 0.06, 48));
    brim.position.y = 0.03;
    g.add(brim);
    // Crown: domed sphere above the brim (slightly flattened vertically).
    const crown = new THREE.Mesh(new THREE.SphereGeometry(0.55, 40, 20));
    crown.scale.set(1, 0.9, 1);
    crown.position.y = 0.55;
    g.add(crown);
    return g;
  }

  // Build a CAR (Three.js, Y-up — mirrors the Blender 'car' in scene.py.template
  // so preview and pack agree): low body box + raised cabin + 4 wheels, facing +X.
  function buildCarGeometry() {
    const g = new THREE.Group();
    // Body: long, low box (x=length, y=height, z=width), centred at y=0.35.
    const body = new THREE.Mesh(new THREE.BoxGeometry(1.9, 0.45, 0.8));
    body.position.y = 0.35;
    g.add(body);
    // Cabin: shorter, narrower box on top, slightly forward.
    const cabin = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.4, 0.7));
    cabin.position.set(0.10, 0.70, 0);
    g.add(cabin);
    // Four wheels: short cylinders under the body (axle along Z = side-to-side).
    [[0.62, 0.40], [0.62, -0.40], [-0.62, 0.40], [-0.62, -0.40]].forEach(function (w) {
      const wheel = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.10, 20));
      wheel.rotation.x = Math.PI / 2;      // cylinder axis Y -> Z (axle across)
      wheel.position.set(w[0], 0.16, w[1]);
      g.add(wheel);
    });
    return g;
  }

  // Per-sample geometry: the picked sample drives the 3D shape (hat -> hat,
  // car -> car, cup -> cup, dog -> stylized dog, wood -> wood slab).
  // Unknown -> cup (the default).
  function buildShapeGeometry(shape) {
    if (shape === 'dog') return buildDogGeometry();
    if (shape === 'wood') return buildWoodGeometry();
    if (shape === 'hat') return buildHatGeometry();
    if (shape === 'car') return buildCarGeometry();
    return buildCupGeometry();
  }

  // Remove whatever is in the 3D preview and return it to the empty state
  // (shown on page load and whenever a new reference/sample is picked).
  function clearPreview() {
    if (!preview.ready || !preview.scene) return;
    preview.isGlb = false;
    if (preview.mesh) {
      preview.scene.remove(preview.mesh);
      preview.mesh.traverse(function (o) {
        if (o.isMesh && o.geometry) o.geometry.dispose();
      });
      preview.mesh = null;
    }
    setPreviewNote('3D preview appears after Generate.');
  }

  // Swap the preview object for the one matching the picked sample (keeps the
  // current material, so textures still apply to the new shape). v14.4 mirrors
  // the Blender pack's layout: TWO variants side-by-side — `*_Albedo` (flat
  // PBR, no height) on the left and `*_Relief` (PBR + height bump) on the
  // right — exactly as scene.py places them at x = -1.3 / +1.3.
  function setPreviewShape(shape) {
    if (!preview.ready || !preview.scene) return;
    preview.isGlb = false;
    if (preview.mesh) {
      preview.scene.remove(preview.mesh);
      preview.mesh.traverse(function (o) {
        if (o.isMesh && o.geometry) o.geometry.dispose();
      });
    }
    const g = buildShapeGeometry(shape);
    const left = g.clone(true);
    const right = g.clone(true);
    // Albedo variant: full PBR maps, NO height. Relief variant: + height bump.
    left.traverse(function (o) { if (o.isMesh) o.material = preview.matAlbedo; });
    right.traverse(function (o) { if (o.isMesh) o.material = preview.matRelief; });
    const pair = new THREE.Group();
    left.position.x = -0.95;
    right.position.x = 0.95;
    pair.add(left);
    pair.add(right);
    // Frame the pair like the Blender pack (two objects side by side): scale
    // so the whole pair fits the orbit view and centre it on the target.
    const box = new THREE.Box3().setFromObject(pair);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const scale = 2.6 / maxDim;
    pair.scale.setScalar(scale);
    pair.position.set(-center.x * scale, -0.3 - center.y * scale, -center.z * scale);
    preview.scene.add(pair);
    preview.mesh = pair;
    setPreviewNote('Albedo (left) · Relief (right) — same as the Blender pack');
  }

  // ------------------------------------------------- image-derived 3D (OBJ)
  // The backend can build a mesh FROM THE IMAGE (rembg silhouette + Depth-
  // Anything relief) instead of a preset shape. It emits a strict-subset OBJ
  // (v / vt / f v/v quads, vertex index == uv index), so a tiny inline parser
  // is enough — no need to pull in an OBJLoader build.
  function parseOBJ(text) {
    const lines = String(text).split(/\r?\n/);
    const pos = [], uv = [], idx = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line || line[0] === '#') continue;
      const p = line.split(/\s+/);
      const tag = p[0];
      if (tag === 'v') {
        pos.push(parseFloat(p[1]), parseFloat(p[2]), parseFloat(p[3]));
      } else if (tag === 'vt') {
        uv.push(parseFloat(p[1]), (p[2] !== undefined && p[2] !== '') ? parseFloat(p[2]) : 0);
      } else if (tag === 'f') {
        for (let j = 1; j < p.length; j++) {
          const vi = parseInt(p[j].split('/')[0], 10);
          if (vi) idx.push(vi - 1); // vertex index == uv index in our OBJ
        }
      }
    }
    return { pos: pos, uv: uv, idx: idx };
  }

  function buildObjGeometry(text) {
    const d = parseOBJ(text);
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(d.pos, 3));
    g.setAttribute('uv', new THREE.Float32BufferAttribute(d.uv, 2));
    g.setIndex(d.idx);
    // Center + scale to a fixed on-screen size (the mesh is ~2 x 2 x ~1.2).
    g.computeBoundingBox();
    if (g.boundingBox) {
      const size = new THREE.Vector3();
      const center = new THREE.Vector3();
      g.boundingBox.getSize(size);
      g.boundingBox.getCenter(center);
      g.translate(-center.x, -center.y, -center.z);
      const s = 1.9 / Math.max(size.x, size.y, size.z, 1e-6);
      g.scale(s, s, s);
    }
    g.computeVertexNormals();
    return g;
  }

  // Replace the preview object with the image-derived 3D model from `objUrl`.
  // v14.4: a `.glb` (real 3D, Hunyuan3D-2.1) is loaded with THREE.GLTFLoader and
  // KEEPS its baked PBR materials (base color/normal/metallic/roughness); an
  // `.obj` (the 2.5D relief fallback) is parsed inline and gets the live PBR
  // preview materials (matAlbedo + matRelief variants). Returns a promise that
  // resolves once the mesh is in the scene. Falls back to the preset shape on
  // any load/parse failure.
  function setPreviewModel(objUrl, fallbackShape) {
    if (!preview.ready || !preview.scene) return Promise.resolve();
    const isGlb = /\.glb(\?|$)/i.test(objUrl);
    const onFail = function (e) {
      setPreviewNote('3D preview: model unavailable (' + e.message + ') — showing preset shape');
      setPreviewShape(fallbackShape || state.shape || 'cup');
    };
    if (isGlb && THREE && THREE.GLTFLoader) {
      return loadGlb(api(objUrl), onFail);
    }
    return fetch(api(objUrl), { cache: 'no-store' })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.text();
      })
      .then(function (text) {
        const g = buildObjGeometry(text);
        if (!g.index || g.index.count === 0) throw new Error('empty mesh');
        // v14.4: mirror the Blender pack — TWO variants side-by-side
        // (`*_Albedo` flat PBR on the left, `*_Relief` PBR+height bump right).
        const meshA = new THREE.Mesh(g, preview.matAlbedo);
        const meshR = new THREE.Mesh(g, preview.matRelief);
        const pair = new THREE.Group();
        meshA.position.x = -0.95;
        meshR.position.x = 0.95;
        pair.add(meshA);
        pair.add(meshR);
        preview.isGlb = false;
        if (preview.mesh) {
          preview.scene.remove(preview.mesh);
          preview.mesh.traverse(function (o) { if (o.isMesh && o.geometry) o.geometry.dispose(); });
        }
        preview.scene.add(pair);
        preview.mesh = pair;
        _placeModel(pair, -0.3);
        setPreviewNote('Albedo (left) · Relief (right) — 3D from image (silhouette + depth)');
      })
      .catch(onFail);
  }

  // Center a loaded model at the origin, scale it to a comfortable preview
  // size, and drop it slightly below the orbit target (y offset).
  function _placeModel(obj, yOffset) {
    const box = new THREE.Box3().setFromObject(obj);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const scale = 1.6 / maxDim;
    obj.scale.setScalar(scale);
    obj.position.set(-center.x * scale, yOffset - center.y * scale, -center.z * scale);
  }

  function loadGlb(url, onFail) {
    const loader = new THREE.GLTFLoader();
    return new Promise(function (resolve, reject) {
      loader.load(url,
        function (gltf) {
          let root = gltf.scene || gltf.scenes[0];
          if (!root) { reject(new Error('GLB has no scene')); return; }
          // Flatten: a Hunyuan3D GLB is usually a single mesh under a group.
          if (preview.mesh) {
            preview.scene.remove(preview.mesh);
            preview.mesh.traverse(function (o) { if (o.isMesh && o.geometry) o.geometry.dispose(); });
          }
          preview.scene.add(root);
          preview.mesh = root;
          preview.isGlb = true;
          _placeModel(root, 0);
          setPreviewNote('Real 3D model (Hunyuan3D-2.1) — textured mesh, drag to orbit');
          resolve();
        },
        undefined,
        function (err) { reject(err); }
      );
    }).catch(onFail);
  }

  function initPreview() {
    const canvas = $('preview-canvas');
    if (!canvas || typeof THREE === 'undefined' || !THREE.Scene) {
      setPreviewNote('3D preview unavailable in this environment.');
      return;
    }
    try {
      const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
      renderer.setPixelRatio((typeof window !== 'undefined' && window.devicePixelRatio) || 1);
      if (THREE.sRGBEncoding !== undefined) renderer.outputEncoding = THREE.sRGBEncoding;

      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x0d1117);

      const camera = new THREE.PerspectiveCamera(50, 640 / 420, 0.1, 100);

      // two directional lights + ambient
      scene.add(new THREE.AmbientLight(0xffffff, 0.45));
      const key = new THREE.DirectionalLight(0xffffff, 0.95);
      key.position.set(3, 4, 2);
      scene.add(key);
      const fill = new THREE.DirectionalLight(0xffffff, 0.4);
      fill.position.set(-3, 2, -2);
      scene.add(fill);

      // Empty scene on load — the 3D preview stays EMPTY until Generate is
      // clicked (or an asset is loaded from the library), matching the user's
      // requested flow. Two variant materials mirror the Blender pack:
      // matAlbedo = flat PBR (no height), matRelief = PBR + height bump.
      preview.matAlbedo = new THREE.MeshStandardMaterial({
        roughness: 0.85,
        metalness: 0.05,
        side: THREE.DoubleSide,
      });
      preview.matRelief = new THREE.MeshStandardMaterial({
        roughness: 0.85,
        metalness: 0.05,
        side: THREE.DoubleSide,
      });
      preview.material = preview.matRelief; // legacy alias -> relief variant

      preview.renderer = renderer;
      preview.scene = scene;
      preview.camera = camera;
      preview.ready = true;

      resizePreview();
      bindOrbit(canvas);

      setPreviewNote('3D preview appears after Generate.');

      if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(animate);
      }
    } catch (e) {
      setPreviewNote('3D preview failed to initialize: ' + e.message);
    }
  }

  function resizePreview() {
    if (!preview.renderer) return;
    const canvas = $('preview-canvas');
    const w = (canvas && (canvas.clientWidth || canvas.width)) || 640;
    const h = (canvas && (canvas.clientHeight || canvas.height)) || 420;
    preview.renderer.setSize(w, h, false);
    preview.camera.aspect = w / h;
    preview.camera.updateProjectionMatrix();
  }

  function updateCamera() {
    if (!preview.camera) return;
    const r = preview.radius, t = preview.theta, p = preview.phi;
    preview.camera.position.set(
      r * Math.sin(p) * Math.sin(t),
      r * Math.cos(p),
      r * Math.sin(p) * Math.cos(t)
    );
    preview.camera.lookAt(0, 0, 0);
  }

  function animate() {
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(animate);
    if (preview.renderer) {
      updateCamera();
      preview.renderer.render(preview.scene, preview.camera);
    }
  }

  function bindOrbit(canvas) {
    canvas.addEventListener('mousedown', function (e) {
      preview.dragging = true;
      preview.lastX = e.clientX;
      preview.lastY = e.clientY;
    });
    canvas.addEventListener('wheel', function (e) {
      e.preventDefault();
      preview.radius = clamp(preview.radius * Math.pow(1.0015, e.deltaY), 1.5, 8);
    }, { passive: false });
    if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
      window.addEventListener('mousemove', function (e) {
        if (!preview.dragging) return;
        const dx = e.clientX - preview.lastX;
        const dy = e.clientY - preview.lastY;
        preview.lastX = e.clientX;
        preview.lastY = e.clientY;
        preview.theta -= dx * 0.005;
        preview.phi = clamp(preview.phi - dy * 0.005, 0.05, Math.PI - 0.05);
      });
      window.addEventListener('mouseup', function () { preview.dragging = false; });
      window.addEventListener('resize', resizePreview);
    }
  }

  function loadTexture(url, srgb) {
    // srgb defaults to true (albedo); pass false for data maps (normal /
    // roughness / metallic / height) which must stay in linear space.
    return new Promise(function (resolve, reject) {
      const img = new Image();
      img.onload = function () {
        const t = new THREE.Texture(img);
        const isSrgb = srgb !== false;
        if (isSrgb) {
          if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
          else if (THREE.SRGBColorSpace !== undefined) t.colorSpace = THREE.SRGBColorSpace;
        }
        t.wrapS = t.wrapT = THREE.RepeatWrapping;
        t.needsUpdate = true;
        resolve(t);
      };
      img.onerror = function () { reject(new Error('could not load texture: ' + url)); };
      img.src = url;
    });
  }

  // Load the full PBR map set for an asset and apply it to the preview
  // material. Each map is loaded independently so a missing/404 map degrades
  // gracefully instead of failing the whole update. (Live update after
  // generation or when an asset is loaded from the library.)
  async function setPreviewMaps(opts) {
    if (!preview.ready || typeof Image === 'undefined') return;
    // A real-3D GLB carries its OWN baked PBR textures (correct UVs). Do NOT
    // overwrite them with the flat procedural maps (their UVs wouldn't match
    // the 3D unwrap).
    if (preview.isGlb) return;
    const id = opts.id;
    const paths = {
      albedo: opts.albedo || (id ? '/api/assets/' + id + '/maps/albedo.png' : null),
      height: opts.height || (id ? '/api/assets/' + id + '/maps/height.png' : null),
      normal: id ? '/api/assets/' + id + '/maps/normal.png' : null,
      roughness: id ? '/api/assets/' + id + '/maps/roughness.png' : null,
      metallic: id ? '/api/assets/' + id + '/maps/metallic.png' : null,
    };
    const load = function (p, srgb) {
      if (!p) return Promise.resolve(null);
      return loadTexture(api(p), srgb).catch(function () { return null; });
    };
    try {
      const [tA, tH, tN, tR, tM] = await Promise.all([
        load(paths.albedo, true),
        load(paths.height, false),
        load(paths.normal, false),
        load(paths.roughness, false),
        load(paths.metallic, false),
      ]);
      if (preview.texAlbedo) preview.texAlbedo.dispose();
      if (preview.texHeight) preview.texHeight.dispose();
      if (preview.texNormal) preview.texNormal.dispose();
      if (preview.texRoughness) preview.texRoughness.dispose();
      if (preview.texMetallic) preview.texMetallic.dispose();
      preview.texAlbedo = tA;
      preview.texHeight = tH;
      preview.texNormal = tN;
      preview.texRoughness = tR;
      preview.texMetallic = tM;

      const mA = preview.matAlbedo;
      const mR = preview.matRelief;
      // Albedo variant: full PBR (albedo/normal/roughness/metallic), NO height.
      mA.map = tA || mA.map;
      mA.normalMap = tN || null;
      mA.roughnessMap = tR || null;
      mA.metalnessMap = tM || null;
      if (tA || tN || tR || tM) mA.needsUpdate = true;
      // Relief variant: same PBR set + height as BUMP (Blender's Bump node,
      // Distance = HEIGHT_SCALE; bump not displacement keeps the silhouette).
      mR.map = tA || mR.map;
      if (tN) { mR.normalMap = tN; mR.normalScale = new THREE.Vector2(1.0, 1.0); }
      else mR.normalMap = null;
      if (tR) { mR.roughnessMap = tR; mR.roughness = 1.0; }
      else { mR.roughnessMap = null; mR.roughness = 0.85; }
      if (tM) { mR.metalnessMap = tM; mR.metalness = 1.0; }
      else { mR.metalnessMap = null; mR.metalness = 0.05; }
      if (tH) mR.bumpMap = tH;
      else mR.bumpMap = null;
      const dispEl = $('disp-slider');
      const v = parseFloat(dispEl && dispEl.value);
      mR.bumpScale = isFinite(v) ? v : 0.05;
      mR.needsUpdate = true;
      setPreviewNote('Albedo (left) · Relief (right) — same as the Blender pack');
    } catch (e) {
      setPreviewNote('Preview update failed: ' + e.message);
    }
  }

  // --------------------------------------------------------- reference column
  const PRESETS = {
    cup: 'Convert this reference (a speckled ceramic cup) into a clean ceramic glaze texture map: preserve the cream base color and the multicolor speckle pattern, glossy low-roughness glaze, evenly lit, no objects, no background, no perspective, no shading artifacts.',
    dog: 'Convert this reference (a tricolor merle dog) into a clean, flat fur texture map: preserve the long soft double-coat pattern with white, tan and slate-gray merle coloring and fine dark ticking, evenly lit, no objects, no background, no perspective, no shading artifacts.',
    wood: 'Convert this reference into a clean, flat weathered wood grain texture map: preserve the horizontal gray-brown grain, cracks and weathered silvery patches, evenly lit, no objects, no background, no perspective, no shading artifacts.',
    generic: 'Convert this reference image into a clean, flat, evenly-lit texture map suitable for 3D modeling. Preserve the material\u2019s color, pattern and detail. Output a flat texture with no objects, no background, no perspective, no shading artifacts.',
    hat: 'Convert this reference (a hat) into a clean, flat fabric texture map: preserve the hat material, weave and color, evenly lit, no objects, no background, no perspective, no shading artifacts.',
    car: 'Convert this reference (a car) into a clean, flat car-body paint texture map: preserve the body color, metallic flakes and glossy clear-coat finish, evenly lit, no objects, no background, no perspective, no shading artifacts.',
  };

  function getPrompt() {
    const sel = $('preset-select');
    const v = (sel && sel.value) ? sel.value : 'generic';
    if (v === 'custom') {
      const c = $('custom-prompt');
      return (c && c.value) ? c.value.trim() : '';
    }
    return PRESETS[v] || PRESETS.generic;
  }

  function showRefPreview(ref) {
    const img = $('ref-preview');
    const status = $('ref-status');
    const obj = ref.file || ref.blob;
    if (obj && typeof URL !== 'undefined' && URL.createObjectURL) {
      img.src = URL.createObjectURL(obj);
      img.hidden = false;
    }
    const name = ref.file ? ref.file.name : (ref.filename || 'reference');
    const size = (ref.file && ref.file.size) || (ref.blob && ref.blob.size) || 0;
    status.hidden = false;
    status.textContent = name + (size ? ' (' + (size / 1048576).toFixed(2) + ' MB)' : '');
  }

  function clearSampleSelection() {
    const grid = $('sample-grid');
    if (grid && grid.querySelectorAll) {
      grid.querySelectorAll('.sample-thumb').forEach(function (el) {
        el.classList.remove('selected');
      });
    }
  }

  function updateGenerateButton() {
    const btn = $('generate-btn');
    if (btn) btn.disabled = !state.reference || state.generating;
  }

  function handleFile(file) {
    if (file.type && file.type !== 'image/png' && file.type !== 'image/jpeg') {
      toast('Only PNG or JPG images are supported.');
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      toast('Image too large — maximum size is 8 MB.');
      return;
    }
    state.reference = { file: file };
    clearSampleSelection();
    // An upload isn't one of the bundled samples — its 3D shape comes from
    // the selected material preset (Cup/Hat/Car, default cup). The preview
    // stays EMPTY until Generate is clicked.
    state.shape = currentPresetShape();
    clearPreview();
    showRefPreview(state.reference);
    updateGenerateButton();
  }

  function bindUpload() {
    const dz = $('dropzone');
    const fi = $('file-input');
    if (!dz || !fi) return;
    dz.addEventListener('click', function () { fi.click(); });
    ['dragenter', 'dragover'].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault();
        dz.classList.add('drag');
      });
    });
    ['dragleave', 'dragend'].forEach(function (ev) {
      dz.addEventListener(ev, function () { dz.classList.remove('drag'); });
    });
    dz.addEventListener('drop', function (e) {
      e.preventDefault();
      dz.classList.remove('drag');
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) handleFile(file);
    });
    fi.addEventListener('change', function () {
      const file = fi.files && fi.files[0];
      if (file) handleFile(file);
      fi.value = '';
    });
  }

  // Map a bundled sample to the material preset that generates it best.
  // Clicking a sample auto-selects its preset so "Generate" is one click away.
  const SAMPLE_PRESET = {
    sample_hat: 'hat',
    sample_car: 'car',
    sample_cup: 'cup',
  };

  // Map the material preset to the matching 3D shape (uploads use the
  // selected preset; bundled samples use their API-provided shape).
  const PRESET_SHAPE = {
    cup: 'cup',
    hat: 'hat',
    car: 'car',
    dog: 'dog',
    wood: 'wood',
    generic: 'cup',
    custom: 'cup',
  };

  function currentPresetShape() {
    const ps = $('preset-select');
    const v = (ps && ps.value) ? ps.value : 'generic';
    return PRESET_SHAPE[v] || 'cup';
  }

  async function loadSampleReference(s) {
    try {
      const resp = await fetch(api(s.url), { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const blob = await resp.blob();
      state.reference = { blob: blob, filename: s.url.split('/').pop() || (s.id + '.png') };
      // Auto-select the matching material preset (e.g. Cup -> "Cup" preset).
      // The 3D preview stays EMPTY until Generate is clicked (v14.4).
      const ps = $('preset-select');
      if (ps && SAMPLE_PRESET[s.id]) ps.value = SAMPLE_PRESET[s.id];
      const shape = s.shape || SAMPLE_PRESET[s.id] || 'cup';
      state.shape = shape;
      clearPreview();
      showRefPreview(state.reference);
      updateGenerateButton();
    } catch (e) {
      toast('Could not load sample: ' + e.message);
    }
  }

  async function loadSamples() {
    const grid = $('sample-grid');
    if (!grid) return;
    grid.innerHTML = 'Loading samples&hellip;';
    try {
      const resp = await fetch(api('/api/samples'), { cache: 'no-store' });
      const list = await parseJSON(resp, 'GET /api/samples');
      state.samples = Array.isArray(list) ? list : [];
      renderSamples();
    } catch (e) {
      grid.innerHTML = 'Failed to load samples: ' + esc(e.message);
    }
  }

  function renderSamples() {
    const grid = $('sample-grid');
    if (!grid) return;
    grid.innerHTML = '';
    if (!state.samples.length) {
      grid.innerHTML = 'No samples available.';
      return;
    }
    state.samples.forEach(function (s) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sample-thumb';
      const img = document.createElement('img');
      img.src = api(s.url);
      img.alt = s.name || s.id;
      const label = document.createElement('span');
      label.textContent = s.name || s.id;
      btn.appendChild(img);
      btn.appendChild(label);
      btn.addEventListener('click', function () {
        clearSampleSelection();
        btn.classList.add('selected');
        loadSampleReference(s);
      });
      grid.appendChild(btn);
    });
  }

  // ------------------------------------------------------------- generate flow
  async function generate() {
    if (state.generating || !state.reference) return;
    state.generating = true;
    const btn = $('generate-btn');
    if (btn) { btn.disabled = true; btn.classList.add('busy'); }
    const hint = $('gen-hint');
    if (hint) hint.hidden = false;
    let sentName = '', sentTag = '';
    try {
      const fd = new FormData();
      const ref = state.reference;
      if (ref.file) fd.append('reference', ref.file);
      else if (ref.blob) fd.append('reference', ref.blob, ref.filename);

      const nameEl = $('asset-name');
      sentName = nameEl && nameEl.value ? nameEl.value.trim() : '';
      if (sentName) fd.append('name', sentName);
      const tagEl = $('asset-tag');
      sentTag = tagEl && tagEl.value ? tagEl.value.trim() : '';
      if (sentTag) fd.append('tag', sentTag);

      const prompt = getPrompt();
      if (prompt) fd.append('prompt', prompt);

      // 3D shape for the preview / Blender pack (driven by the picked sample
      // or the material preset for uploads).
      fd.append('shape', state.shape || 'cup');

      const strengthEl = $('strength-slider');
      const sv = strengthEl && strengthEl.value !== '' ? strengthEl.value : '0.7';
      fd.append('strength', String(clamp(parseFloat(sv) || 0.7, 0.3, 0.95)));

      const seedEl = $('seed-input');
      const seed = seedEl && seedEl.value ? String(seedEl.value).trim() : '';
      if (seed !== '') fd.append('seed', seed);

      // Image-derived 3D (brief + request D): when the user opts in, the mesh
      // is built from the photo (silhouette + depth) instead of the preset shape.
      const geomEl = $('geom-toggle');
      const doGeom = !!(geomEl && geomEl.checked);
      if (doGeom) fd.append('do_geometry', 'true');

      const resp = await fetch(api('/api/generate'), { method: 'POST', body: fd });
      const result = await parseJSON(resp, 'POST /api/generate');
      if (!result || !result.id) throw new Error('unexpected response from server');
      onGenerateResult(result, sentName, sentTag);
    } catch (e) {
      toast('Generation failed: ' + e.message);
    } finally {
      state.generating = false;
      if (btn) btn.classList.remove('busy');
      if (hint) hint.hidden = true;
      updateGenerateButton();
    }
  }

  function onGenerateResult(r, sentName, sentTag) {
    state.currentAsset = {
      id: r.id,
      name: sentName || ('texture-' + r.id),
      tag: sentTag || '',
      mode: r.mode || 'model',
      albedo_url: r.albedo_url,
      height_url: r.height_url,
      elapsed_ms: r.elapsed_ms,
      shape: r.shape || state.shape || 'cup',
      model_url: r.model_url || null,
      model_type: r.model_type || null,
      geometry_mode: r.geometry_mode || null,
    };
    // Show the object matching the sample that was generated.
    state.shape = state.currentAsset.shape;
    if (state.currentAsset.model_url) {
      // Image-derived mesh (silhouette + depth) — show the real object.
      setPreviewModel(state.currentAsset.model_url, state.shape);
    } else {
      setPreviewShape(state.shape);
    }
    showResult(state.currentAsset);
    setPreviewMaps({ id: state.currentAsset.id, albedo: r.albedo_url, height: r.height_url });
    loadAssets();
  }

  function showResult(a) {
    const at = $('albedo-thumb');
    const ht = $('height-thumb');
    if (at) { at.src = api(a.albedo_url); at.hidden = false; }
    if (ht) { ht.src = api(a.height_url); ht.hidden = false; }
    const ap = $('albedo-ph'); if (ap) ap.hidden = true;
    const hp = $('height-ph'); if (hp) hp.hidden = true;

    const badge = $('mode-badge');
    if (badge) {
      const isModel = a.mode === 'model';
      badge.textContent = isModel ? 'model' : 'fallback-reference';
      badge.className = 'badge ' + (isModel ? 'ok' : 'warn');
      badge.title = isModel
        ? 'Generated by the AI image model'
        : 'image model was unavailable — reference returned as-is';
    }
    const el = $('elapsed');
    if (el) el.textContent = 'elapsed: ' + fmtElapsed(a.elapsed_ms);
    const rn = $('result-name');
    if (rn) rn.textContent = a.name + (a.tag ? ' · ' + a.tag : '');
    const rm = $('result-meta');
    if (rm) rm.hidden = false;

    const dlA = $('dl-albedo');
    if (dlA) { dlA.href = api(a.albedo_url); dlA.download = 'albedo.png'; dlA.hidden = false; }
    const dlH = $('dl-height');
    if (dlH) { dlH.href = api(a.height_url); dlH.download = 'height.png'; dlH.hidden = false; }
    const dlP = $('dl-pack');
    if (dlP) dlP.disabled = false;
    const dlB = $('dl-blend');
    if (dlB) dlB.disabled = false;

    // Geometry (image-derived 3D) badge + download, when a model was built.
    // v14: `model_type` distinguishes the real-3D GLB (Hunyuan3D-2.1) from the
    // 2.5D relief OBJ fallback.
    const gb = $('geom-badge');
    if (gb) {
      if (a.model_url) {
        const isGlb = a.model_type === 'glb' || /\.glb(\?|$)/i.test(a.model_url);
        gb.hidden = false;
        gb.className = 'badge ok';
        gb.textContent = (isGlb ? 'Real 3D · ' : '3D from image · ') + (a.geometry_mode || (isGlb ? 'hunyuan3d' : 'obj'));
        gb.title = isGlb
          ? 'Textured 3D mesh generated by Hunyuan3D-2.1'
          : 'Mesh derived from the photo (silhouette + depth), not a preset shape';
        const dm = $('dl-model');
        if (dm) { dm.href = api(a.model_url); dm.download = isGlb ? 'model.glb' : 'model.obj'; dm.hidden = false; }
      } else {
        gb.hidden = true;
        const dm = $('dl-model');
        if (dm) dm.hidden = true;
      }
    }

    const ni = $('asset-name');
    if (ni) { ni.disabled = false; ni.value = a.name; }
    const ti = $('asset-tag');
    if (ti) { ti.disabled = false; ti.value = a.tag || ''; }
    const sv = $('save-asset');
    if (sv) sv.disabled = false;
  }

  // ----------------------------------------------------------- asset library
  async function loadAssets() {
    const grid = $('asset-grid');
    if (!grid) return;
    grid.innerHTML = '<div class="asset-card loading">Loading assets&hellip;</div>';
    try {
      const resp = await fetch(api('/api/assets'), { cache: 'no-store' });
      const data = await parseJSON(resp, 'GET /api/assets');
      state.assets = (data && Array.isArray(data.assets)) ? data.assets : [];
      renderAssets();
    } catch (e) {
      grid.innerHTML = '<div class="asset-card loading">Failed to load assets: ' + esc(e.message) + '</div>';
    }
  }

  function renderAssets() {
    const grid = $('asset-grid');
    if (!grid) return;
    const se = $('library-search');
    const q = (se && se.value ? se.value : '').trim().toLowerCase();
    grid.innerHTML = '';
    const list = state.assets.filter(function (a) {
      return !q ||
        (a.name || '').toLowerCase().indexOf(q) !== -1 ||
        (a.tag || '').toLowerCase().indexOf(q) !== -1;
    });
    if (!list.length) {
      const d = document.createElement('div');
      d.className = 'asset-card loading';
      d.textContent = state.assets.length
        ? 'No assets match your search.'
        : 'No assets yet — generate your first texture.';
      grid.appendChild(d);
      return;
    }
    list.forEach(function (a) { grid.appendChild(buildAssetCard(a)); });
  }

  function buildAssetCard(a) {
    const card = document.createElement('div');
    card.className = 'asset-card';
    card.dataset.id = a.id;

    const img = document.createElement('img');
    img.className = 'card-thumb';
    img.src = api(a.thumb);
    img.alt = a.name || a.id;
    card.appendChild(img);

    const titleRow = document.createElement('div');
    titleRow.className = 'card-title-row';
    const h = document.createElement('h3');
    h.textContent = a.name || a.id;
    const badge = document.createElement('span');
    const isModel = a.mode === 'model';
    badge.className = 'badge ' + (isModel ? 'ok' : 'warn');
    badge.textContent = isModel ? 'model' : 'fallback-reference';
    badge.title = isModel
      ? 'Generated by the AI image model'
      : 'image model was unavailable — reference returned as-is';
    titleRow.appendChild(h);
    titleRow.appendChild(badge);
    if (a.has_model) {
      const g3 = document.createElement('span');
      g3.className = 'badge ok';
      g3.textContent = '3D';
      g3.title = 'Image-derived 3D model available (silhouette + depth)';
      titleRow.appendChild(g3);
    }
    card.appendChild(titleRow);

    const meta = document.createElement('div');
    meta.className = 'card-meta-row';
    if (a.tag) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = a.tag;
      meta.appendChild(chip);
    }
    const date = document.createElement('span');
    date.className = 'dim';
    date.textContent = fmtDate(a.created);
    meta.appendChild(date);
    card.appendChild(meta);

    const edit = document.createElement('div');
    edit.className = 'card-edit-row';
    const nIn = document.createElement('input');
    nIn.type = 'text';
    nIn.value = a.name || '';
    nIn.placeholder = 'name';
    const tIn = document.createElement('input');
    tIn.type = 'text';
    tIn.value = a.tag || '';
    tIn.placeholder = 'tag';
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn small';
    save.textContent = 'Save';
    save.addEventListener('click', function () {
      setBusy(save, true);
      saveAsset(a.id, nIn.value.trim(), tIn.value.trim())
        .finally(function () { setBusy(save, false); });
    });
    edit.appendChild(nIn);
    edit.appendChild(tIn);
    edit.appendChild(save);
    card.appendChild(edit);

    const acts = document.createElement('div');
    acts.className = 'card-actions';

    const maps = document.createElement('button');
    maps.type = 'button';
    maps.className = 'btn small';
    maps.textContent = 'Maps';
    maps.addEventListener('click', function () {
      if (typeof window === 'undefined' || typeof window.open !== 'function') return;
      window.open(api(a.thumb), '_blank');
      window.open(api(heightUrl(a.id)), '_blank');
    });

    const pack = document.createElement('button');
    pack.type = 'button';
    pack.className = 'btn small';
    pack.textContent = 'Blender-pack';
    pack.addEventListener('click', function () {
      setBusy(pack, true);
      downloadPack(a.id).catch(function (err) {
        toast('Blender-pack download failed: ' + err.message);
      }).finally(function () { setBusy(pack, false); });
    });

    const load = document.createElement('button');
    load.type = 'button';
    load.className = 'btn small';
    load.textContent = 'Load into preview';
    load.addEventListener('click', function () {
      loadAssetIntoPreview(a);
    });

    acts.appendChild(maps);
    acts.appendChild(pack);
    acts.appendChild(load);
    card.appendChild(acts);
    return card;
  }

  async function saveAsset(id, name, tag) {
    const resp = await fetch(api('/api/assets/' + id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, tag: tag }),
    });
    const updated = await parseJSON(resp, 'PATCH /api/assets/' + id);
    const idx = state.assets.findIndex(function (x) { return x.id === id; });
    if (idx !== -1 && updated) {
      state.assets[idx] = Object.assign({}, state.assets[idx], updated);
    }
    if (state.currentAsset && state.currentAsset.id === id && updated) {
      state.currentAsset.name = updated.name || name;
      state.currentAsset.tag = updated.tag || tag;
    }
    renderAssets();
    toast('Asset saved.', 'info');
  }

  // Render an existing asset's maps in the 3D preview without regenerating.
  function loadAssetIntoPreview(a) {
    state.currentAsset = {
      id: a.id,
      name: a.name || a.id,
      tag: a.tag || '',
      mode: a.mode || 'model',
      albedo_url: a.thumb,
      height_url: heightUrl(a.id),
      shape: a.shape || 'cup',
      model_url: a.model_url || (a.has_model ? '/api/assets/' + a.id + '/model.obj' : null),
      model_type: a.model_type || null,
    };
    // Show the object this asset was generated as (its stored shape).
    state.shape = state.currentAsset.shape;
    if (state.currentAsset.model_url) {
      setPreviewModel(state.currentAsset.model_url, state.shape);
    } else {
      setPreviewShape(state.shape);
    }
    showResult(state.currentAsset);
    setPreviewMaps({ id: a.id, albedo: a.thumb, height: heightUrl(a.id) });
    switchTab('generate');
  }

  // ------------------------------------------------------------- downloads
  function triggerDownload(blob, filename) {
    const a = document.createElement('a');
    let url = '';
    if (typeof URL !== 'undefined' && URL.createObjectURL) url = URL.createObjectURL(blob);
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    if (url && typeof URL !== 'undefined' && URL.revokeObjectURL) {
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    }
  }

  async function downloadPack(id) {
    const resp = await fetch(api('/api/assets/' + id + '/blender-pack'));
    if (!resp.ok) throw new Error('HTTP ' + resp.status + ' ' + (resp.statusText || ''));
    const blob = await resp.blob();
    triggerDownload(blob, id + '-blender-pack.zip');
  }

  // Download a ready-to-open .blend (built by the sidecar from the same
  // scene.py the add-on's Load Pack runs — so it is byte-equivalent to the
  // artist's). First click builds (~10-60s, button shows "Building…"); later
  // clicks stream the cached file (instant). A 503 = a build is already in
  // flight; we surface that instead of silently failing.
  async function downloadBlend(id, btn) {
    const label = btn && btn.textContent ? btn.textContent : null;
    try {
      if (btn) { btn.disabled = true; btn.textContent = 'Building .blend … (first time)'; }
      const resp = await fetch(api('/api/assets/' + id + '/blend'));
      if (!resp.ok) {
        let msg = 'HTTP ' + resp.status;
        try { const j = await resp.json(); if (j.detail) msg = j.detail; } catch (e) { /* non-JSON */ }
        throw new Error(msg);
      }
      const blob = await resp.blob();
      triggerDownload(blob, id + '.blend');
      toast('.blend downloaded — opens directly in Blender.', 'info');
    } catch (err) {
      toast('.blend download failed: ' + err.message, 'error');
    } finally {
      if (btn && label) { btn.textContent = label; }
      if (btn) btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------- tabs
  function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset && b.dataset.tab === name);
    });
    const tg = $('tab-generate');
    const tl = $('tab-library');
    if (tg) tg.hidden = name !== 'generate';
    if (tl) tl.hidden = name !== 'library';
    if (name === 'library') loadAssets();
    if (name === 'generate') resizePreview();
  }

  function bindTabs() {
    document.querySelectorAll('.tab-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        if (b.dataset && b.dataset.tab) switchTab(b.dataset.tab);
      });
    });
  }

  // -------------------------------------------------------------------- init
  async function loadHealth() {
    try {
      const resp = await fetch(api('/api/health'), { cache: 'no-store' });
      const h = await parseJSON(resp, 'GET /api/health');
      const banner = $('model-banner');
      if (banner) banner.hidden = !(h && h.image_model === 'down');
    } catch (e) {
      toast('Health check failed: ' + e.message);
    }
  }

  function bindControls() {
    const ps = $('preset-select');
    if (ps) {
      ps.addEventListener('change', function () {
        const cf = $('custom-prompt-field');
        if (cf) cf.hidden = ps.value !== 'custom';
        // Changing the material preset only records the shape for Generate —
        // the 3D preview stays EMPTY until Generate is clicked.
        state.shape = currentPresetShape();
      });
    }
    const ss = $('strength-slider');
    if (ss) {
      const sv = $('strength-value');
      ss.addEventListener('input', function () {
        const v = parseFloat(ss.value);
        if (sv) sv.textContent = isFinite(v) ? Math.round(v * 100) + '%' : '70%';
      });
    }
    const ds = $('disp-slider');
    if (ds) {
      const dv = $('disp-value');
      ds.addEventListener('input', function () {
        const v = parseFloat(ds.value);
        if (dv) dv.textContent = isFinite(v) ? v.toFixed(2) : '0.05';
        // Drives the Relief variant's Bump node equivalent (bumpScale, not
        // true displacement — matches the Blender pack's Bump node).
        if (preview.matRelief) preview.matRelief.bumpScale = isFinite(v) ? v : 0.05;
      });
    }
    const saveBtn = $('save-asset');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        if (!state.currentAsset) return;
        setBusy(saveBtn, true);
        saveAsset(state.currentAsset.id,
          ($('asset-name').value || '').trim(),
          ($('asset-tag').value || '').trim())
          .catch(function (e) { toast('Save failed: ' + e.message); })
          .finally(function () { setBusy(saveBtn, false); });
      });
    }
    const packBtn = $('dl-pack');
    if (packBtn) {
      packBtn.addEventListener('click', function (e) {
        if (e && e.preventDefault) e.preventDefault();
        if (!state.currentAsset) return;
        setBusy(packBtn, true);
        downloadPack(state.currentAsset.id)
          .catch(function (err) { toast('Blender-pack download failed: ' + err.message); })
          .finally(function () { setBusy(packBtn, false); });
      });
    }
    const blendBtn = $('dl-blend');
    if (blendBtn) {
      blendBtn.addEventListener('click', function (e) {
        if (e && e.preventDefault) e.preventDefault();
        if (!state.currentAsset) return;
        downloadBlend(state.currentAsset.id, blendBtn);
      });
    }
    const genBtn = $('generate-btn');
    if (genBtn) genBtn.addEventListener('click', function () { generate(); });
    const search = $('library-search');
    if (search) search.addEventListener('input', function () { renderAssets(); });
  }

  function init() {
    try { bindTabs(); } catch (e) { console.error('tab binding failed', e); }
    try { bindUpload(); } catch (e) { console.error('upload binding failed', e); }
    try { bindControls(); } catch (e) { console.error('control binding failed', e); }
    try { initPreview(); } catch (e) { console.error('preview init failed', e); }
    loadHealth();
    loadSamples();
    loadAssets();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Minimal test hooks (harmless in the browser).
  if (typeof window !== 'undefined') {
    window.__vizDemo = {
      init: init,
      state: state,
      generate: generate,
      loadAssets: loadAssets,
      loadSamples: loadSamples,
      loadHealth: loadHealth,
      setPreviewMaps: setPreviewMaps,
      setPreviewShape: setPreviewShape,
      buildShapeGeometry: buildShapeGeometry,
      saveAsset: saveAsset,
      downloadPack: downloadPack,
      downloadBlend: downloadBlend,
      parseJSON: parseJSON,
      api: api,
      API_BASE: API_BASE,
      PRESETS: PRESETS,
    };
  }
})();

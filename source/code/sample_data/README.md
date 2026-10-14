# Sample reference images

**These are REAL reference photos** (supplied by the user, 2026-10-03) — the
left "Sample library" lists exactly these three. Each is also a preset for the
material prompt (`SAMPLE_NAMES` in `backend/main.py`, `PRESETS` / `SAMPLE_PRESET`
in `frontend/app.js`), so clicking a sample auto-selects its preset for a
one-click Generate. The old placeholders (marble / bust / fabric / synthetic
cup) were removed in favour of these.

| File | Sample | Content / texture reference |
|---|---|---|
| `sample_cup.png`  | Cup  | A speckled cream **ceramic cup** (glossy glaze, multicolor speckle, handle + rim + base) — daily object. |
| `sample_dog.jpg`  | Dog  | A tricolor **merle Australian Shepherd** in a meadow — long soft double-coat **fur** reference. |
| `sample_wood.jpg` | Wood | **Weathered wood grain** (horizontal gray-brown grain, cracks, silvery patches) — flat wood albedo reference. |

Keep the filenames — the frontend and tests reference them by name.
If you swap in new images, keep the same filename and update the matching
entry in `SAMPLE_NAMES` and, if needed, the `PRESETS` prompt.

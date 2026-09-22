# Material Bakery

**Step-by-step texture baking for Blender 4.x / 5.x.**

A Blender add-on that separates **baking** from **delivery**. Most bake add-ons are
fire-and-forget; here you pick targets and maps, bake, *look at the result*, and only then
decide what to do with it — export textures, build one material per collection, clean up
material slots, or duplicate the objects into a "final" collection. Getting it wrong costs
you a click, not another full bake.

This repository is the **standalone build**: it has no third-party retopology integration.
(The QuadRemesher-driven *Remesh* step exists only in a separate private build.)

---

## 中文

**Blender 的分步烘焙插件。** 把**烘焙**和**交付**拆成两件事：先挑目标和贴图、烘、
**看结果**，再决定怎么用 —— 导出贴图、按集合建材质、清理材质槽、复制最终物体。
不满意只花你一次点击，不用重烘。

本仓库是**独立版本**：不含任何第三方重拓补集成（QuadRemesher 的 *Remesh* 步骤
只存在于另一个私有构建里）。

---

## Highlights

- **One texture set per collection.** A collection named `Common Parts 1` produces a
  material called `Common Parts 1` and maps called `Common Parts 1-BaseColor-2k.png`,
  `-Normal`, `-Metallic`, `-Roughness`, …
- **Your UVs are never touched.** No packing, no new UV layer, no re-projection. Objects
  are baked through the UVs they already have — deliberately overlapping UVs included.
  (`MBAKERY_UV` is only ever created if *you* ask for it on the Settings page.)
- **Non-destructive material delivery.** A baked material is *appended* as an extra slot;
  originals stay untouched. *Preview* assigns the faces, *Finalize For Export* keeps only
  the baked slot. Both directions are reversible from a backup stored on the object.
- **46 bake types**, UDIM support, Selected-to-Active projection from a high-poly,
  per-channel sample counts (deterministic channels at 1 sample, AO/shadow at 64 by
  default).
- **It does not freeze after a bake.** Global undo and auto-save are paused for the
  duration of a run — a multi-GB auto-save write right after the last map is what makes
  Blender *look* hung — and both are restored on every exit path.
- **The log says where the time went.** Every phase reports its own duration
  (`dispatch <map>: done in 10.24s`), and any file write is recorded too.

## Install

```powershell
# sync the working tree into the shipping folder, then build the zip
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_package.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_zips.ps1
```

Install `MaterialBakery\material_bakery_install.zip` via
`Edit > Preferences > Add-ons > Install...`, enable **Material Bakery**, then press `N` in
the 3D viewport.

**Fully restart Blender after installing.** `Install...` only writes files to disk; the copy
already loaded in memory is not replaced. Page 1 shows `v<version> · build <timestamp>` —
that stamp exists precisely so you can tell which build you are running.

## Workflow

1. **Targets & Maps** — choose collections / a collection / selected objects, and the maps.
2. **Settings** — naming, output folder, quality, margin, antialiasing, UDIM, UV prep.
3. **Bake** — queue, live status, pause / resume / cancel at any time.
4. **Result** — export textures, build materials, add/preview/finalize slots, create final
   objects, save a report.

## Repository layout

| Path | What |
|---|---|
| `MaterialBakery/material_bakery/` | The add-on itself — this is what the zip is built from |
| `MaterialBakery/README.md` | User manual (behaviour, edge cases, known limits) |
| `MATERIAL_BAKERY_DESIGN.md` | Design notes: why every decision was made, with measured numbers |
| `tests/` | Headless regression suites — `tests/README.md` maps them |
| `tools/` | Packaging, sync and measurement scripts |
| `material_bakery/` | Working tree — edit here, then sync into `MaterialBakery/` |
| `test.blend` | Fixture for the real-Cycles bake suites |

## Tests

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1
```

Suites report `PASS`, `SKIP` (self-declared) or `FAIL`, and the summary lists per-suite
timings plus the grand total — currently **15 suites / 1130 checks, 0 failures, ~25 s** on
Blender 4.5.13 LTS. The workspace path is derived from each script's own location, so a
clone runs out of the box.

## Licence

GPL v2 or later — see `MaterialBakery/LICENSE.txt`.

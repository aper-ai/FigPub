# Figure Workbench implementation contract

Scope: integrate GelFigure and ScieFig only. No FigScan, no BlotFigure, no network AI APIs. Local Windows-first app. Refactor into Python analysis/storage/export core plus dependency-free browser ES modules. Preserve attribution and AGPL-3.0-only obligations for ScieFig-derived code. Do not market unmeasured industry-leading performance.

Version: 0.2.0. Authoritative typed models live in `workbench/models.py`; source-plane semantics live in `workbench/planes.py`. Application runtime logic is a new implementation, not an imported upstream GUI or analysis engine.

## Canonical project JSON

```
{
 "schema_version":1,"id":"uuid","title":"Untitled Figure",
 "revision":0,"created_at":"ISO","updated_at":"ISO",
 "assets":[{"id":"full lowercase sha256","sha256":"full","name":"original.tif","kind":"image|svg|pdf|table","width_px":1024,"height_px":768,"bit_depth":16,"dtype":"uint16","channels":1,"frames":1,"metadata":{},"preview_url":"/api/assets/ID/preview"}],
 "figure":{"width_mm":180,"height_mm":120,"background":"#ffffff"},
 "panels":[{"id":"uuid","asset_id":"id","x_mm":10,"y_mm":10,"width_mm":75,"height_mm":50,"label":"A","crop":{"x":0,"y":0,"width":1024,"height":768},"rotation":0,"flip_h":false,"flip_v":false,"display":{"low":null,"high":null,"gamma":1,"invert":false},"gel":{"lane_centers":[],"lane_names":[],"marker_points":[],"marker_unit":"kDa","hidden_lanes":[]},"title":""}],
 "annotations":[{"id":"uuid","type":"text|line|rect","x_mm":10,"y_mm":10,"width_mm":10,"height_mm":5,"text":"","font_size_pt":8,"color":"#222222"}],
 "measurements":[],"audit":[],"settings":{"min_ppi":300,"font_family":"DejaVu Sans"}
}
```

All source coordinates are original pixels. Geometry changes do not modify source bytes. Millimeters are canonical for the final canvas. `display` never affects numerical analyses. Asset paths are server-private; API uses IDs. All project-originals must be included in portable `.figwork` ZIP. Plot datasets and analysis recipe/source identities travel in project. Imported/exported sources preserve full bytes; revisions create audit records.

## HTTP API (all JSON except noted)

- `GET /api/health` -> {ok,version,capabilities}
- `GET /api/project` -> project JSON
- `PUT /api/project` body entire project -> saved project (use revision check to prevent stale writes; client serializes saves)
- `POST /api/project/new` body {title?} -> fresh project
- `POST /api/demo` -> replace current with explicitly labelled synthetic DEMO project, assets and multiple panels; no random scientific results presented as real
- `POST /api/assets` multipart `files` -> {assets:[...]} and add to project (frontend then GET project)
- `GET /api/assets/{id}/preview?max_size=1200&page=0&low=&high=&gamma=1&invert=false` -> PNG; cache by immutable hash and parameters
- `GET /api/assets/{id}/original` -> original file
- `GET /api/assets/{id}/pixels?max_size=1024` -> {width,height,source_width,source_height,data:[0..1 floats],bit_depth}; grayscale localization only; analysis always raw
- `POST /api/jobs` body {kind:"wb|qpcr|microscopy",params:{...}} -> {id,status}
- `GET /api/jobs/{id}` -> {id,status:"queued|running|done|error|cancelled",result?,error?,elapsed_ms?}; stale/cancelled results must not commit into a newer project
- `DELETE /api/jobs/{id}` -> cancellation request
- `POST /api/preflight` body {project?:project,min_ppi?:300} -> {ok,issues:[{severity:"error|warning|info",code,message,panel_id?,asset_id?,details?}],metrics:{...}}
- `POST /api/export` body {format:"pdf|svg|png|tiff|pptx|sources|project",dpi:300,outlined_text:false,project?:project} -> download bytes (client fetch then blob download)
- `POST /api/project/open` multipart `file` `.figwork` -> loaded project; reject traversal/oversized zip/hash mismatch/missing references
- `GET /api/benchmarks` -> cached measured benchmark results, optional until benchmark has run

## Analysis function contracts (no Qt imports)

`workbench.analysis.analyze_wb(images: dict[str,np.ndarray], params: dict) -> dict`

`params.rois`: [{id,asset_id,x,y,width,height,sample_id,group,biological_replicate,technical_replicate,exposure_id,role:"target|control",protein}]. `background`: "border|rolling_ball|none". `polarity`: "dark|bright". `radius` optional. Result {kind:"wb",rows:[...],groups:[...],warnings:[],method,elapsed_ms}. Keep full precision, raw dtype saturation, explicit control matching, don't count exposures as independent n. Accept/validate empty ROI and errors clearly.

`workbench.qpcr.analyze_qpcr(rows:list[dict], params:dict)->dict`

CSV/XLSX rows fields sample,target,ct,group,biological_replicate,technical_replicate. params reference_gene,control_group,method:"delta_delta_ct". Result {kind:"qpcr",rows,groups,warnings,method}. Technical repeats aggregate within biological sample before group statistics, no invented replicates.

`workbench.microscopy.analyze_microscopy(image:np.ndarray,params:dict)->dict`

params {method:"nuclei|ihc|intensity|channel_intensity|threshold|line_profile|channel_overlap|cellpose",source?:SourcePlane,channels?:[{id,name,source,...}],threshold?:number,min_area:20,um_per_px?:number,roi?:Region,...}. Return {kind:"microscopy",count?,objects?,rows?,pairs?,profiles?,summary,warnings,method}. The model adapter is implemented in a separate CPU process. Availability and pinned models are actually checked; cancellation terminates the worker. IHC requires one true RGB source, without a scalar channels array.

Server resolves asset arrays, table rows, passes functions, persists completed result only when source revision still valid. Numeric result objects must be JSON safe, never emit NaN/Infinity.

## Export contracts (root implements)

`workbench.exporting.export_project(project:dict,asset_resolver:callable,format:str,dpi:int=300,outlined_text:bool=False)->tuple[bytes,str,str]` returns bytes, mime, filename. resolver(asset_id)->Path. Sources/project delegate storage archive if appropriate. Root implements PDF/SVG/PNG/TIFF/PPTX with vector text/shapes, raw-resolution image cropping, font embedding, source PDF vector composition. SVG sanitized against executable/external content.

`workbench.preflight.check_project(project,asset_resolver,min_ppi=300)->dict` per-source pixel resolutions including embedded SVG/PDF rasters, original source coverage, invalid coordinates/saturation, min font/canvas bounds. No authenticity guarantee from hashes.

## UI direction

Chinese polished desktop workbench; calm off-white/graphite with restrained teal accent, typography clear, no gradient/glass, use inline SVG icons. 3-column workspace: assets at left, large stage center, context inspector right. Top workflow tabs (素材/凝胶与WB/分析/组图), global undo/redo/save/export. All displayed controls operational. Source/figure split or switch; mouse ROI and numeric edit; select-drag-resize panel on SVG scene; snapped alignment/fit zoom; keyboard (Ctrl+Z/Y/S, Delete) with input focus guards. Display real per-panel PPI; human errors and asynchronous status. Demo must visibly say 示例数据. Persist autosave; `.figwork` import/export; cancellation; selection retained after saves. Accessibility labels and empty/loading/error states. No fake UI buttons, fake success, fake computed scientific numbers.

Frontend gel detection: `new Worker('/gel-worker.js')`, post {id,image:{width,height,source_width,source_height,data},lane_count?,mode:'dna|protein'} -> {id,result:{lane_centers,bands,marker_points?,...}} or error. Worker uses the new `web/gel-engine.js` implementation. Only the attributed Marker specification catalog is carried over as data, not upstream algorithm code.


## Multichannel source and display contract

```json
{
  "version": 1,
  "mode": "split",
  "active_channel": "c0",
  "split": {"columns": 0, "gap_px": 8, "include_merge": true},
  "channels": [{
    "id": "c0", "name": "DAPI", "color": "#0000FF", "visible": true,
    "black": null, "white": null, "gamma": 1,
    "source": {"asset_id": "FULL_SHA256", "series": 0, "frame": 0, "channel": 0}
  }]
}
```

The same `channel_view` belongs to a panel or `settings.asset_edits[asset_id]`. A group can reference different originals and different native dtypes. The maximum is 32 selected scalar planes per view/analysis, not an assertion that files only contain 32 channels. Plane dimensions must agree; no registration, projection, dtype promotion or resampling is implied by a group.

`frame` flattens non-Y/X/C/S axes in C order; frame coordinates explicitly describe each axis/index/size. The scalar channel component flattens C then S. RGB sample roles distinguish image components from independent scientific channels. `read_plane(source)` returns a read-only 2D array with original dtype. `read_array(asset_id,page=0,series=0,frame=None)` is the compatibility full-frame reader; a non-null frame takes precedence over page.

Names, colors, visibility, gamma, display levels and layout are display properties. Analysis builds its own source descriptors and scientific parameters. Scientific signatures do not include LUT settings or display visibility. No display-normalized values enter raw intensity measurement.

A shared tile manifest gives total width/height, the exact rounded raw crop, and `tiles` with `id,channel_id,channel_ids,mode,name,x_px,y_px,width_px,height_px,source?`. This makes source ROI mapping, export and effective PPI use the same geometry. Split panels cannot also splice hidden gel lanes; split into independent panels first. A blank merge with all channels hidden is allowed but explicitly flagged by preflight.

### HTTP additions

- `GET /api/assets/{id}/channels?series=0&frame=0`: series list, axes, frame coordinates, scalar channel metadata/ranges, default_view, truncation information.
- `POST /api/channel-view/describe` with `{channel_view,crop?}`: canonical channel_view and layout.
- `POST /api/channel-view/preview` with `{channel_view,crop?,max_size:1600}`: PNG bytes. Maximum size 32–4096, bounded cache, concurrent same-key requests coalesced.
- `POST /api/channel-view/auto-range` with `{channel_view,percentiles:[1,99.5],channel_ids?:[]}`: updated channel_view plus statistics/levels/warnings. Sampling status and sample_count are explicit.
- Existing preview/pixels/pixels.bin accept series, frame and channel. The binary localization endpoint returns little-endian Float32 pixels with width/height/source size headers; this is not a numerical-analysis input.
- `GET /api/assets/{id}/qpcr?sheet=NAME&limit=1000`: actual canonical qPCR rows from the same worksheet analyzed by the job; targets, groups, sample_groups, sheet names, original source_row, header_row, total_rows and truncation.
- `GET /api/cellpose/status?refresh=false`: actual optional runtime/model readiness. A status check may start an isolated dependency probe; health only uses known cached status.

### qPCR and WB statistics

qPCR accepts reference_genes, group_mapping, excluded_source_rows, max_ct, technical_sd_limit, sheet and test. A group mapping first matches `experiment_id::sample`, then the legacy sample key. CSV included values false/no/0 actually exclude the well. Exclusions refer to original worksheet row numbers and remain recipe changes; original CSV/XLSX bytes are not edited.

Tests are none (default), welch_t, student_t, paired_t and anova_tukey. qPCR comparisons use independently aggregated biological ΔCt. WB uses its independently aggregated pre-fold measurement value and accepts control_group. Families are isolated by experiment plus target/protein. Paired IDs must match biological_replicate; missing IDs are reported. Student/ANOVA assume equal variance; Welch does not. Tests are explicit, not inferred from a desired significance result.

Comparisons return group1/group2, n1/n2, optional n_pairs/paired IDs/unmatched IDs, test, basis, statistic, p_value_raw, p_value, adjustment, family_size and note. Pairwise t tests against the control use Holm adjustment within a family. Tukey p_value is already adjusted and p_value_raw is null. ANOVA overall results are in omnibus. Insufficient n, zero variance or undefined estimates are null with an explanation.

### Microscopy extensions

Rectangular, elliptical and polygon ROIs use raw source pixels and an explicit pixel-center mask. Background uses an explicit original ROI or fixed value. Line profiles retain original-coordinate endpoints, line width, sampling and calibration. Raw sums use float64 and keep signed background-corrected values.

Channel rows retain source identity, raw_mean/raw_integrated, background_mean, corrected_mean/corrected_integrated and optional positive_area_px/positive_fraction. Channel pairs return Pearson, Jaccard, Dice, overlap areas/fractions and Manders metrics with threshold and denominator semantics. Constant intensity and empty denominators stay null. Profiles return distance_px/distance_um, values and valid_width_samples.

Cellpose model settings are a whitelist: model cyto3/nuclei, diameter_px, min_area, flow_threshold, cellprob_threshold, niter, threads and normalize. Only packaged pinned official weights are loaded. The parent passes read-only source information and an isolated array file; the child returns labels and model metadata. The parent validates shape/IDs and computes actual original-pixel object measurements. No arbitrary executable/model path is accepted from a project.

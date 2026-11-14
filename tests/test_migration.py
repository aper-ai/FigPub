"""Fixtures mirror the two upstream save formats; no upstream runtime is used."""
import base64
import gzip
import hashlib
import io
import json
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import numpy as np
import pytest
import tifffile

from workbench.migration import migrate_legacy
from workbench.storage import ProjectStore, validate_project


def test_sciefig_channel_names_colors_ranges_visibility_and_split_are_restored():
    output = io.BytesIO()
    array = np.arange(3 * 24 * 32, dtype=np.uint16).reshape(3, 24, 32)
    tifffile.imwrite(output, array, photometric='minisblack', metadata={'axes': 'CYX'})
    raw = output.getvalue()
    archive_name = 'data/fluorescence/fluo/0000_img/source.tif'
    def update(project, composer, files):
        files[archive_name] = raw
        project['panels'] = [{'panel_uuid': 'fluo', 'panel_id': 'Fluorescence', 'panel_type': 'fluorescence', 'data': {
            'fluorescence_images': [{'uid': 'img', 'source_name': 'source.tif', 'source_hash': 'sha256:' + sha(raw),
                'embedded_copy_path': archive_name, 'view_mode': 'split', 'channels': [
                    {'index': 0, 'name': 'DAPI', 'pseudocolor': '#0000FF', 'display_min': 100, 'display_max': 1000, 'visible': True},
                    {'index': 1, 'name': 'FITC', 'pseudocolor': '#00FF00', 'display_min': 200, 'display_max': 2000, 'visible': False},
                    {'index': 2, 'name': 'Cy3', 'pseudocolor': '#FF0000', 'display_min': 500, 'display_max': 3000, 'visible': True,
                     'display_processing': {'black_point': .1, 'white_point': .9}}]}]}}]
        composer['panel_refs'] = [{'panel_uid': 'fluo', 'x_mm': 10, 'y_mm': 10, 'width_mm': 140, 'height_mm': 60}]
    result = migrate_legacy(sfig_bytes(mutate=update), 'multichannel.sfig')
    panel = result.project['panels'][0]
    view = panel['channel_view']
    assert view['mode'] == 'split' and view['split']['include_merge']
    assert [c['name'] for c in view['channels']] == ['DAPI', 'FITC', 'Cy3']
    assert [c['color'] for c in view['channels']] == ['#0000FF', '#00FF00', '#FF0000']
    assert [c['visible'] for c in view['channels']] == [True, False, True]
    assert view['channels'][2]['black'] == 750 and view['channels'][2]['white'] == 2750
    assert [c['source']['channel'] for c in view['channels']] == [0, 1, 2]
    assert all(c['source']['asset_id'] == sha(raw) for c in view['channels'])
    assert next(s.data for s in result.sources if s.asset_id == sha(raw)) == raw


def tiff_bytes(offset=0):
    out = io.BytesIO()
    tifffile.imwrite(out, (np.arange(1200, dtype=np.uint16).reshape(30, 40) + offset), photometric="minisblack")
    return out.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_hash(value):
    return "sha256:" + sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def gel_bytes(compressed=True, mutate=None):
    source = tiff_bytes()
    payload = {
        "gelfigProject": 1, "app": "GelFigure 1.0.16", "savedAt": "2026-11-11T00:00:00Z",
        "source": {"name": "gel.tif", "sha256": sha(source), "bytes": base64.b64encode(source).decode()},
        "ladders": [], "initial": {}, "autoRotation": 0,
        "state": {"file": {"name": "gel.tif", "bits": 16}, "orient": "", "rotation": 0,
                  "levels": {"black": .1, "white": .8, "gamma": 1.2, "invert": True},
                  "crop": {"x": 4, "y": 5, "w": 30, "h": 20}, "gelType": "dna",
                  "lanes": [{"x": 9, "name": "Marker"}, {"x": 20, "name": "Sample 1"}],
                  "markers": [{"lane": 0, "unit": "kb", "bands": [{"y": 12, "bp": 1000, "show": True}]}],
                  "boxes": [{"x": 10, "y": 11, "w": 5, "h": 3}],
                  "labels": [{"text": "Original label", "pos": "below", "at": .5}],
                  "style": {"gelWidthMm": 60, "autoWidth": False, "panelLetter": "A"}},
    }
    if mutate:
        mutate(payload)
    encoded = json.dumps(payload).encode()
    return gzip.compress(encoded) if compressed else encoded


def sfig_bytes(mutate=None, tamper=None, extras=None):
    first, second = tiff_bytes(), tiff_bytes(123)
    name_a, name_b = "data/wb/wb-panel/strips/strip_1.tif", "data/external/ext-panel/second.tif"
    project = {
        "schema_version": "1.0.0", "project_uuid": "old-project", "title": "Two real sources",
        "panels": [
            {"panel_uuid": "wb-panel", "panel_id": "WB1", "panel_type": "western_blot", "data": {
                "band_strips": [{"strip_id": "strip_1", "embedded_image_path": name_a,
                                 "image_hash": "sha256:" + sha(first), "image_path": "/do/not/open/source.tif",
                                 "protein_name": "GAPDH", "crop_roi": {"x": 2, "y": 3, "w": 20, "h": 10},
                                 "adjustments": {"rotation": 0, "flip_horizontal": True}}]},
             "analysis": {"old_8bit_numbers": [2, 3, 4]}},
            {"panel_uuid": "ext-panel", "panel_id": "Microscopy", "panel_type": "external_image", "data": {
                "raw_source": "second.tif", "raw_hash": "sha256:" + sha(second),
                "raw_copy_path": name_b, "image_path": "C:\\legacy\\second.tif"}},
        ],
        "composer_figures": [{"figure_uid": "figure-a", "figure_id": "Figure1", "file": "composer/Figure1.json"}],
        "active_figure_uid": "figure-a",
    }
    composer = {
        "figure_uid": "figure-a", "figure_id": "Figure1",
        "canvas": {"width_mm": 180, "height_mm": 100, "background_color": "#ffffff"},
        "panel_refs": [
            {"panel_uid": "wb-panel", "x_mm": 10, "y_mm": 12, "width_mm": 60, "height_mm": 30, "figure_label": "A"},
            {"panel_uid": "ext-panel", "x_mm": 90, "y_mm": 12, "width_mm": 50, "height_mm": 40, "figure_label": "B"},
        ],
        "annotations": [
            {"type": "text", "x_mm": 18, "y_mm": 65, "properties": {"content": "Editable label", "font_size_pt": 9}},
            {"type": "shape", "x_mm": 30, "y_mm": 80, "properties": {"shape_type": "line", "width_mm": 12, "height_mm": 6,
                                                                       "x1": 12, "y1": 0, "x2": 0, "y2": 6}},
        ],
    }
    files = {name_a: first, name_b: second}
    if mutate:
        mutate(project, composer, files)
    files["project.json"] = json.dumps(project, ensure_ascii=False).encode()
    files["composer/Figure1.json"] = json.dumps(composer, ensure_ascii=False).encode()
    manifest = {"schema_version": "1.0.0", "project_json_hash": json_hash(project),
                "composer_json_hashes": {"composer/Figure1.json": json_hash(composer)},
                "raw_hashes": {name: "sha256:" + sha(value) for name, value in files.items() if name.startswith("data/")}}
    files["manifest.json"] = json.dumps(manifest).encode()
    if tamper:
        tamper(files)
    if extras:
        files.update(extras)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            # ZipInfo's constructor normalizes backslashes on Windows and
            # truncates NULs everywhere. Preserve the exact attack bytes so
            # this fixture exercises the importer on every host.
            info = ZipInfo(name)
            info.filename = info.orig_filename = name
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, payload)
    return output.getvalue()


@pytest.mark.parametrize("compressed", [False, True])
def test_gelfigure_original_precision_crop_levels_and_annotations(compressed):
    result = migrate_legacy(gel_bytes(compressed), "figure.gelfig")
    assert len(result.sources) == 1
    assert result.sources[0].data == tiff_bytes()
    assert result.sources[0].asset_id == sha(tiff_bytes())
    project = result.project
    validate_project(project)
    panel = project["panels"][0]
    assert panel["crop"] == {"x": 4, "y": 5, "width": 30, "height": 20}
    assert panel["width_mm"] == 60 and panel["height_mm"] == 40
    assert panel["display"]["low"] == pytest.approx(6553.5)
    assert panel["display"]["high"] == pytest.approx(52428)
    assert project["assets"][0]["bit_depth"] == 16
    assert panel["gel"]["lane_centers"] == [9, 20]
    assert panel["gel"]["marker_points"] == [{"y": 12, "size": 1}]
    assert panel["gel"]["marker_unit"] == "kb"
    box = project["annotations"][0]
    assert (box["x_mm"], box["y_mm"], box["width_mm"], box["height_mm"]) == (27, 30, 10, 6)
    snapshot = json.loads(project["settings"]["migration"]["legacy_snapshot_json"])
    assert "bytes" not in snapshot["source"]
    assert project["measurements"] == []


def test_gelfigure_rotated_frame_is_not_mislabelled_as_original_coordinates():
    result = migrate_legacy(gel_bytes(mutate=lambda value: value["state"].update(rotation=2.5)), "rotate.gelfig")
    panel = result.project["panels"][0]
    assert panel["crop"] is None and panel["rotation"] == 0
    assert panel["gel"]["lane_centers"] == []
    assert not panel["legacy"]["coordinates_mapped"]
    assert any("原图坐标" in warning for warning in result.warnings)
    assert json.loads(result.project["settings"]["migration"]["legacy_snapshot_json"])["state"]["rotation"] == 2.5


def test_gelfigure_rejects_corrupt_original_hash_and_nonfinite_or_duplicate_json():
    with pytest.raises(ValueError, match="hash mismatch"):
        migrate_legacy(gel_bytes(mutate=lambda value: value["source"].update(sha256="0" * 64)), "bad.gelfig")
    with pytest.raises(ValueError, match="non-finite"):
        migrate_legacy(b'{"gelfigProject":1,"test":1e999}', "bad.gelfig")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        migrate_legacy(b'{"gelfigProject":1,"gelfigProject":1}', "bad.gelfig")


def test_sciefig_multisource_native_crops_mm_layout_and_editable_text():
    result = migrate_legacy(sfig_bytes(), "project.sfig")
    assert {source.data for source in result.sources} == {tiff_bytes(), tiff_bytes(123)}
    project = result.project
    validate_project(project)
    assert project["figure"] == {"width_mm": 180, "height_mm": 100, "background": "#ffffff"}
    assert len(project["panels"]) == 2
    wb, microscopy = project["panels"]
    assert wb["crop"] == {"x": 2, "y": 3, "width": 20, "height": 10}
    assert wb["flip_h"] and (wb["x_mm"], wb["y_mm"], wb["width_mm"], wb["height_mm"]) == (10, 12, 60, 30)
    assert microscopy["asset_id"] != wb["asset_id"]
    assert project["annotations"][0]["text"] == "Editable label"
    line = project["annotations"][1]
    assert (line["x_mm"], line["y_mm"], line["width_mm"], line["height_mm"]) == (42, 80, -12, 6)
    assert project["measurements"] == []
    assert "old_8bit_numbers" in project["settings"]["migration"]["legacy_snapshot_json"]


@pytest.mark.parametrize("member", ["project.json", "composer/Figure1.json", "data/external/ext-panel/second.tif"])
def test_sciefig_rejects_hash_tampering(member):
    def tamper(files):
        if member.endswith(".json"):
            payload = json.loads(files[member])
            payload["tampered"] = True
            files[member] = json.dumps(payload).encode()
        else:
            files[member] = tiff_bytes(456)
    with pytest.raises(ValueError, match="hash mismatch"):
        migrate_legacy(sfig_bytes(tamper=tamper), "bad.sfig")


@pytest.mark.parametrize("member", ["../escaped", "/absolute", "C:/escape", "C:escape",
                                    "data\\escape", "data\\..\\escaped", "\\\\server\\share",
                                    "data/escape\x00hidden", "data//escape", "data/./escape"])
def test_sciefig_rejects_archive_path_traversal(member):
    payload = sfig_bytes(extras={member: b"bad"})
    with ZipFile(io.BytesIO(payload)) as archive:
        assert member in [info.orig_filename for info in archive.infolist()]
    with pytest.raises(ValueError, match="Unsafe"):
        migrate_legacy(payload, "bad.sfig")


def test_sciefig_rejects_symlink():
    archive_bytes = io.BytesIO(sfig_bytes())
    with ZipFile(archive_bytes, "a") as archive:
        info = ZipInfo("data/link.tif")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        migrate_legacy(archive_bytes.getvalue(), "bad.sfig")


def test_sciefig_npy_is_not_unpickled_and_external_reference_is_not_opened(tmp_path):
    outside = tmp_path / "must-not-read.tif"
    outside.write_bytes(tiff_bytes(999))

    def mutate(project, composer, files):
        project["panels"].append({"panel_uuid": "missing", "panel_id": "Missing", "panel_type": "external_image",
                                  "data": {"image_path": str(outside), "raw_source": outside.name}})
        composer["panel_refs"].append({"panel_uid": "missing"})
        # Even a pickle-shaped payload is never loaded. Unsupported originals
        # are reported explicitly instead of executing a deserializer.
        files["data/fluorescence/old/analysis.npy"] = b"\x80\x04cos\nsystem\nnot-executed"

    result = migrate_legacy(sfig_bytes(mutate=mutate), "safe.sfig")
    assert len(result.sources) == 2 and len(result.project["panels"]) == 2
    assert any("analysis.npy" in warning for warning in result.warnings)
    assert any("外部路径" in warning for warning in result.warnings)


def test_sciefig_duplicate_raw_content_deduplicates_without_losing_paths():
    def mutate(project, composer, files):
        files["data/wb/copy-of-first.tif"] = tiff_bytes()
    result = migrate_legacy(sfig_bytes(mutate=mutate), "duplicate.sfig")
    assert len(result.sources) == 2
    first = next(source for source in result.sources if source.asset_id == sha(tiff_bytes()))
    assert len(first.metadata["legacy"]["archive_names"]) == 2


def test_legacy_basename_hash_manifest_is_accepted_only_when_unambiguous():
    def legacy_manifest(files):
        manifest = json.loads(files["manifest.json"])
        manifest["raw_hashes"] = {key.rsplit("/", 1)[-1]: value for key, value in manifest["raw_hashes"].items()}
        files["manifest.json"] = json.dumps(manifest).encode()
    result = migrate_legacy(sfig_bytes(tamper=legacy_manifest), "legacy.sfig")
    assert len(result.sources) == 2


def test_project_store_imports_and_reopens_all_legacy_originals(tmp_path):
    store = ProjectStore(tmp_path / "first")
    project = store.import_legacy(sfig_bytes(), "old.sfig")
    assert len(project["assets"]) == 2 and project["revision"] == 0
    archive = store.export_archive()
    reopened_store = ProjectStore(tmp_path / "second")
    reopened = reopened_store.open_archive(archive)
    assert len(reopened["assets"]) == 2
    for asset in reopened["assets"]:
        assert sha(reopened_store.resolve(asset["id"]).read_bytes()) == asset["id"]
    before = store.get_project()
    with pytest.raises(ValueError):
        store.import_legacy(gel_bytes(mutate=lambda value: value["source"].update(sha256="0" * 64)), "bad.gelfig")
    assert store.get_project() == before


def test_microscopy_same_basename_multisource_and_anisotropic_calibration():
    def mutate(project, composer, files):
        project["panels"] = [{"panel_uuid": "micro", "panel_type": "ihc", "panel_id": "IHC", "data": {
            "ihc_images": [
                {"uid": "first", "source_name": "same.tif", "source_path": "C:\\old\\same.tif",
                 "measurement_scale_bar": {"um_per_px_x": .4, "um_per_px_y": .6, "calibration_source": "metadata"},
                 "scale_bar": {"enabled": True, "length_um": 5, "color": "#ffffff"}},
                {"uid": "second", "source_name": "same.tif", "source_path": "/old/other/same.tif"},
            ]}}]
        files.clear()
        files["data/ihc/micro/0000_first/same.tif"] = tiff_bytes()
        files["data/ihc/micro/0001_second/same.tif"] = tiff_bytes(567)
        composer["panel_refs"] = [{"panel_uid": "micro", "x_mm": 8, "y_mm": 15, "width_mm": 70, "height_mm": 60}]
    result = migrate_legacy(sfig_bytes(mutate=mutate), "microscopy.sfig")
    assert len(result.sources) == 2
    first, second = result.project["panels"]
    assert first["asset_id"] == sha(tiff_bytes()) and second["asset_id"] == sha(tiff_bytes(567))
    assert first["calibration"]["um_per_px_x"] == .4
    assert first["calibration"]["um_per_px_y"] == .6
    assert first["scale_bar"] == {"um_per_px": .4, "length_um": 5, "color": "#ffffff"}
    assert first["y_mm"] == 15 and second["y_mm"] == 45
    assert any("均分" in warning for warning in result.warnings)


def test_corrupt_embedded_decoder_output_is_a_validation_error():
    def corrupt_source(project, composer, files):
        files["data/broken.pdf"] = b"%PDF-1.7\ninvalid document"
    with pytest.raises(ValueError):
        migrate_legacy(sfig_bytes(mutate=corrupt_source), "broken.sfig")


def test_gzip_expansion_is_bounded(monkeypatch):
    import workbench.migration as migration
    monkeypatch.setattr(migration, "MAX_GEL_JSON_BYTES", 128)
    payload = gzip.compress(b'{"gelfigProject":1,"padding":"' + b"x"*20000 + b'"}')
    with pytest.raises(ValueError, match="size limit"):
        migration.migrate_legacy(payload, "bomb.gelfig")


def test_malformed_legacy_geometry_rejects_without_type_error():
    with pytest.raises(ValueError, match="JSON object"):
        migrate_legacy(gel_bytes(mutate=lambda value: value["state"].update(levels=[1, 2, 3])), "bad.gelfig")
    with pytest.raises(ValueError, match="numeric range"):
        migrate_legacy(gel_bytes(mutate=lambda value: value["state"].update(rotation=10**400)), "bad.gelfig")

"""Portable scientific chart labels and source-linked numerical fidelity."""
import copy
import json
import warnings
import xml.etree.ElementTree as ET

import fitz
import pytest

from workbench.charts import analysis_chart
from workbench.qpcr import analyze_qpcr


SVG = "{http://www.w3.org/2000/svg}"
DC = "{http://purl.org/dc/elements/1.1/}"


@pytest.fixture(scope="module", autouse=True)
def font_cache(tmp_path_factory):
    directory = tmp_path_factory.mktemp("chart-font-cache")
    patch = pytest.MonkeyPatch()
    patch.setenv("FIGURE_WORKBENCH_CACHE", str(directory))
    yield directory
    patch.undo()


def measured_chinese_qpcr():
    rows = []
    for sample, group, target_ct, reference_ct in [
        ("c1", "对照组", 20, 18), ("c2", "对照组", 20.3, 18.1),
        ("t1", "处理组", 19, 18), ("t2", "处理组", 19.2, 18.1),
    ]:
        for target, ct in [("目标基因", target_ct), ("内参基因", reference_ct)]:
            rows.append({"sample": sample, "target": target, "ct": ct,
                         "group": group, "biological_replicate": sample})
    params = {"reference_gene": "内参基因", "control_group": "对照组"}
    return analyze_qpcr(rows, params), params


def description(svg):
    document = ET.fromstring(svg)
    return json.loads(document.find(".//" + DC + "description").text)


@pytest.mark.parametrize("chart_type", ["bar", "box", "scatter"])
def test_real_chinese_qpcr_labels_have_outlines_without_missing_font_warnings(chart_type):
    result, params = measured_chinese_qpcr()
    original = copy.deepcopy(result)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        svg = analysis_chart(result, chart_type, provenance={"params": params, "measurement_id": "real-result"})
    assert not any("Glyph" in str(warning.message) for warning in recorded)
    document = ET.fromstring(svg)
    assert document.findall(".//" + SVG + "text") == []
    paths = {element.get("id"): element for element in document.findall(".//" + SVG + "path") if element.get("id")}
    for character in set("目标基因对照组处理组"):
        glyphs = [element for name, element in paths.items() if name.startswith("NotoSansCJKsc-") and name.endswith("-" + format(ord(character), "x"))]
        assert glyphs and all(glyph.get("d") for glyph in glyphs), character
    metadata = description(svg)
    assert metadata["provenance"]["params"] == params
    assert metadata["text_mode"] == "glyph_paths"
    assert metadata["label_editing"] == "regenerate_from_data"
    assert [group["values"] for group in metadata["plot_groups"]] == [group["values"] for group in result["groups"]]
    assert result == original
    # The SVG must render without installed fonts: all labels are path objects.
    with fitz.open(stream=svg, filetype="svg") as source:
        pdf = source.convert_to_pdf()
    with fitz.open(stream=pdf, filetype="pdf") as converted:
        assert converted[0].get_fonts() == []
        assert converted[0].get_images() == []
        pixels = converted[0].get_pixmap()
        assert min(pixels.samples) < 200


def test_numeric_precision_single_observation_and_literal_labels_survive_metadata():
    value = 1.2345678901234567
    result = {"kind": "wb", "groups": [{"group": "对照 $x_1$ \\ lane", "protein": "β-actin",
                                          "values": [value], "metric": "normalized"}]}
    provenance = {"source_asset_ids": ["a"*64], "params": {"background": "border", "radius": 12}, "result_sha256": "b"*64}
    svg = analysis_chart(result, provenance=provenance)
    metadata = description(svg)
    assert metadata["plot_groups"] == [{"label": "β-actin\n对照 $x_1$ \\ lane", "values": [value]}]
    assert metadata["provenance"] == provenance
    assert "n=1: no SD" in metadata["caption"]
    assert "data:image" not in svg.decode()


def test_unsupported_character_fails_clearly_instead_of_to_fallback():
    result = {"kind": "wb", "groups": [{"group": "unsupported \U0001FAE0", "values": [1.0, 2.0]}]}
    with pytest.raises(ValueError, match=r"无法表示.*U\+1FAE0"):
        analysis_chart(result)


def test_missing_bundled_cjk_font_does_not_depend_on_system_fonts(monkeypatch, tmp_path):
    import workbench.charts as charts
    monkeypatch.setattr(charts, "_CJK_COLLECTION", tmp_path / "missing.ttc")
    result, _ = measured_chinese_qpcr()
    with pytest.raises(ValueError, match="字体资源缺失"):
        charts.analysis_chart(result)


def test_persistent_font_cache_and_deterministic_svg(font_cache):
    result, _ = measured_chinese_qpcr()
    first = analysis_chart(result)
    cached = list((font_cache / "fonts").glob("*.otf"))
    assert len(cached) == 1
    stat_before = cached[0].stat()
    second = analysis_chart(result)
    stat_after = cached[0].stat()
    assert first == second
    assert stat_before.st_mtime_ns == stat_after.st_mtime_ns
    assert stat_before.st_size == stat_after.st_size > 1_000_000


def test_box_chart_and_microscopy_use_correct_descriptive_caption():
    box = analysis_chart({"kind": "wb", "groups": [{"group": "A", "values": [1, 2, 3, 4]}]}, "box")
    assert "boxes: IQR" in description(box)["caption"]
    assert "line: median" in description(box)["caption"]
    microscope = analysis_chart({"kind": "microscopy", "objects": [{"area_um2": 5.5}, {"area_um2": 7.2}]}, "scatter")
    assert "not biological replicates" in description(microscope)["caption"]
    assert description(microscope)["y_label"] == "Object area (µm²)"


def test_no_chart_is_created_from_means_without_observations():
    with pytest.raises(ValueError, match="不能从均值制造重复点"):
        analysis_chart({"kind": "wb", "groups": [{"group": "A", "mean": 4.5, "sd": 1.0, "n": 3}]})

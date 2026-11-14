"""Real browser segmentation, instance edits, downloads and project restoration.

All fixtures are synthetic. Browser state is read only for assertions; all edits
use the same buttons, file inputs and original-image clicks as the user.
"""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import tifffile
import uvicorn
from playwright.sync_api import sync_playwright
from workbench.server import create_app
from workbench.instances import decode_instance_mask


def main():
    output = Path(os.environ.get("FW_BROWSER_RESULTS", ROOT / "test-results-instances"))
    output.mkdir(parents=True, exist_ok=True)
    report = {"synthetic_data": True, "scenarios": [], "browser_errors": [], "passed": False}
    yy, xx = np.mgrid[:120, :180]
    raw = (100 + 40000 * np.exp(-((xx - 50) ** 2 + (yy - 50) ** 2) / (2 * 12 ** 2))
           + 25000 * np.exp(-((xx - 135) ** 2 + (yy - 80) ** 2) / (2 * 8 ** 2))).astype(np.uint16)
    stream = io.BytesIO()
    tifffile.imwrite(stream, raw, metadata={"axes": "YX"})
    source_bytes = stream.getvalue()
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    def passed(name):
        report["scenarios"].append({"name": name, "passed": True})
        print("PASS", name, flush=True)

    with tempfile.TemporaryDirectory(prefix="figure-instance-browser-") as directory:
        from run import reserve_port
        listener = reserve_port(0)
        port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(create_app(directory), host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(.025)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
                context = browser.new_context(viewport={"width": 1600, "height": 1100}, accept_downloads=True)
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith((base_url + "/", "data:", "blob:")) else route.abort())
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))

                def value(expression):
                    return page.evaluate("async()=>{const {state,analysisSelection}=await import('/modules/state.js');const {reviewedInstanceResult}=await import('/modules/instance-review.js');return (" + expression + ")}")

                def wait(expression, timeout=25):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if value(expression):
                            return
                        page.wait_for_timeout(30)
                    raise AssertionError("Timed out: " + expression + "\n" + page.locator(".instance-review").inner_text())

                def idle():
                    page.wait_for_function("!document.querySelector('.instance-instruction')?.textContent.includes('正在')")

                def action(name):
                    page.locator(f'[data-instance-action="{name}"]').click()

                def native_click(x, y):
                    svg = page.locator("[data-instance-canvas]")
                    svg.scroll_into_view_if_needed()
                    point = svg.evaluate("(svg,p)=>{const q=new DOMPoint(...p).matrixTransform(svg.getScreenCTM());return {x:q.x,y:q.y}}", [x, y])
                    page.mouse.click(point["x"], point["y"])

                def wait_cursor(cursor):
                    wait(f"state.project.settings.instance_reviews?.[analysisSelection().record?.id]?.cursor==={cursor}")
                    idle()
                    assert not page.locator(".instance-error").count(), page.locator(".instance-review").inner_text()

                def enter_analysis():
                    page.locator('[data-action="tab"][data-tab="analysis"]').click()
                    page.locator('[data-action="analysis-kind"][data-kind="microscopy"]').click()
                    page.locator("#analysis-method").select_option("nuclei")

                try:
                    page.goto(base_url, wait_until="networkidle")
                    page.locator("#import-files").set_input_files({"name": "SYNTHETIC_instance_review.tif", "mimeType": "image/tiff", "buffer": source_bytes})
                    wait("state.project.assets.length===1 && !state.busy")
                    enter_analysis()
                    page.locator("#analysis-threshold").fill("1000")
                    page.locator("#analysis-threshold").press("Tab")
                    page.locator('[data-action="run-analysis"]').click()
                    wait("state.job && ['done','error'].includes(state.job.status)")
                    assert value("state.job.status") == "done", value("state.job")
                    wait("analysisSelection().persisted && !analysisSelection().stale")
                    original = value("analysisSelection().result")
                    assert original["count"] == 2 and original.get("instance_mask")
                    original_labels = decode_instance_mask(original["instance_mask"])
                    original_area = int(np.count_nonzero(original_labels))
                    action("toggle")
                    idle()
                    passed("Uploaded uint16 source runs real classical segmentation and opens lossless instance review")

                    action("split")
                    native_click(42, 50)
                    native_click(58, 50)
                    wait_cursor(1)
                    result = value("reviewedInstanceResult(analysisSelection())")
                    assert result["count"] == 3
                    assert result["instance_review"]["reviewed_foreground_area_px"] == original_area
                    np.testing.assert_array_equal(decode_instance_mask(result["instance_mask"]) > 0, original_labels > 0)
                    action("merge")
                    wait_cursor(2)
                    result = value("reviewedInstanceResult(analysisSelection())")
                    assert result["count"] == 2
                    np.testing.assert_array_equal(decode_instance_mask(result["instance_mask"]), original_labels)
                    passed("Two original-coordinate seed clicks split an instance; merge restores its exact pixels")

                    native_click(4, 4)
                    native_click(135, 80)
                    action("delete")
                    wait_cursor(3)
                    assert value("reviewedInstanceResult(analysisSelection()).count") == 1
                    action("rectangle")
                    svg = page.locator("[data-instance-canvas]")
                    svg.scroll_into_view_if_needed()
                    points = svg.evaluate("svg=>[[150,15],[166,26]].map(p=>{const q=new DOMPoint(...p).matrixTransform(svg.getScreenCTM());return {x:q.x,y:q.y}})")
                    page.mouse.move(points[0]["x"], points[0]["y"])
                    page.mouse.down()
                    page.mouse.move(points[1]["x"], points[1]["y"], steps=6)
                    page.mouse.up()
                    wait_cursor(4)
                    final = value("reviewedInstanceResult(analysisSelection())")
                    assert final["count"] == 2
                    final_labels = decode_instance_mask(final["instance_mask"])
                    assert final_labels[20, 155] == 4 and final_labels[80, 135] == 0
                    assert sum(row["integrated_intensity"] for row in final["objects"]) == int(raw[final_labels > 0].sum())
                    action("undo")
                    wait_cursor(3)
                    assert value("reviewedInstanceResult(analysisSelection()).count") == 1
                    action("redo")
                    wait_cursor(4)
                    assert value("reviewedInstanceResult(analysisSelection()).instance_mask.sha256") == final["instance_mask"]["sha256"]
                    passed("Delete and manual rectangle update original-pixel measurements; undo and redo restore exact masks")

                    with page.expect_download() as download:
                        action("csv")
                    csv_path = output / "reviewed_instances.csv"
                    download.value.save_as(csv_path)
                    assert len(csv_path.read_text("utf-8-sig").splitlines()) == 3
                    with page.expect_download() as download:
                        action("mask")
                    mask_path = output / "reviewed_instances.tif"
                    download.value.save_as(mask_path)
                    with tifffile.TiffFile(mask_path) as file:
                        np.testing.assert_array_equal(file.asarray(), final_labels)
                        metadata = json.loads(file.pages[0].description)
                        assert file.asarray().dtype == np.uint32
                        assert metadata["instance_review"]["cursor"] == 4
                    source_id = value("state.project.assets[0].id")
                    assert hashlib.sha256(page.request.get(base_url + f"/api/assets/{source_id}/original").body()).hexdigest() == source_hash
                    assert value("analysisSelection().result.instance_mask.sha256") == original["instance_mask"]["sha256"]
                    passed("Reviewed CSV and uint32 TIFF match edited masks; original image and automatic segmentation remain unchanged")

                    with page.expect_download() as download:
                        page.locator('[data-action="save-project"]').click()
                    project_path = output / "instance_review.figwork"
                    download.value.save_as(project_path)
                    wait("!state.busy && !state.dirty")
                    page.reload(wait_until="networkidle")
                    page.locator("#open-project").set_input_files(str(project_path))
                    wait("state.modal?.type==='switch-project' && !state.busy")
                    assert value("state.project.assets.length>0"), 'Current workspace must remain intact before confirmation'
                    page.locator('[data-action="confirm-project-switch"]').click()
                    wait("!state.busy && state.project.assets.length===1")
                    enter_analysis()
                    wait("analysisSelection().persisted && !analysisSelection().stale")
                    action("toggle")
                    wait("reviewedInstanceResult(analysisSelection())?.instance_review?.cursor===4")
                    idle()
                    assert value("reviewedInstanceResult(analysisSelection()).instance_mask.sha256") == final["instance_mask"]["sha256"]
                    assert value("analysisSelection().result.instance_mask.sha256") == original["instance_mask"]["sha256"]
                    passed("Portable project reopens, replays all four edits and restores both immutable and reviewed masks")

                    action("add")
                    for x, y in [(150, 50), (165, 50), (150, 64)]:
                        native_click(x, y)
                    action("finish-add")
                    wait_cursor(5)
                    assert value("reviewedInstanceResult(analysisSelection()).count") == 3
                    action("add")
                    for x, y in [(151, 16), (159, 16), (151, 24)]:
                        native_click(x, y)
                    action("finish-add")
                    page.locator(".instance-error", has_text="重叠").wait_for()
                    assert value("state.project.settings.instance_reviews[analysisSelection().record.id].cursor") == 5
                    action("select")
                    action("undo")
                    wait_cursor(4)
                    assert value("reviewedInstanceResult(analysisSelection()).instance_mask.sha256") == final["instance_mask"]["sha256"]
                    passed("Manual polygon editing uses real pixels, rejects overlaps and leaves history unchanged after errors")
                    page.locator("[data-instance-review]").screenshot(path=str(output / "instance-review-restored.png"))
                    assert not report["browser_errors"], report["browser_errors"]
                    report["passed"] = True
                except Exception as error:
                    report["error"] = str(error)
                    page.screenshot(path=str(output / "failure.png"))
                    raise
                finally:
                    (output / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    main()

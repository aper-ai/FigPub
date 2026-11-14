import hashlib
import io
import json
import threading
import time
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

import numpy as np
import pytest
import tifffile
from PIL import Image

from workbench.jobs import JobManager
from workbench.storage import (ByteLRU, ConflictError, ProjectStore, json_bytes,
                               safe_svg, validate_project, _validate_zip)


def tiff_bytes(array):
    out = io.BytesIO()
    tifffile.imwrite(out, array, photometric='minisblack')
    return out.getvalue()


def modify_archive(payload, mutate):
    with ZipFile(io.BytesIO(payload)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    mutate(files)
    out = io.BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as target:
        for name, data in files.items():
            # Preserve malformed member names instead of sanitizing the test
            # input in ZipInfo's platform-dependent constructor.
            info = ZipInfo(name)
            info.filename = info.orig_filename = name
            info.compress_type = ZIP_DEFLATED
            target.writestr(info, data)
    return out.getvalue()


def test_uint16_sources_survive_display_and_portable_multisource_roundtrip(tmp_path):
    store = ProjectStore(tmp_path/'first')
    image_a = np.arange(60000, dtype=np.uint16).reshape(200,300)
    image_b = (np.arange(10000, dtype=np.uint16).reshape(100,100)*5)
    original_a, original_b = tiff_bytes(image_a), tiff_bytes(image_b)
    records = store.add_files([('first.tif',original_a),('second.tif',original_b),('data.csv', b'sample,value\na,1\nb,2\n')])
    a,b,c = records
    project = store.get_project()
    project['panels'] = [store.default_panel(a),store.default_panel(b,'B',100,10,40,40)]
    project['asset_edits'] = {a['id']:{'crop':{'x':10,'y':15,'width':100,'height':80},'custom_extension':'kept'}}
    project['settings']['analysis_rois']=[{'asset_id':a['id'],'x':10,'y':10,'width':12,'height':18}]
    store.update_project(project)
    raw = store.read_array(a['id'])
    assert raw.dtype == np.uint16 and not raw.flags.writeable
    np.testing.assert_array_equal(raw,image_a)
    assert store.preview(a['id'],max_size=100,low=100,high=20000,gamma=1.2,invert=True).startswith(b'\x89PNG')
    assert store.resolve(a['id']).read_bytes() == original_a
    restored = ProjectStore(tmp_path/'second')
    loaded = restored.open_archive(store.export_archive())
    assert len(loaded['assets']) == 3 and len(loaded['panels']) == 2
    assert loaded['asset_edits'][a['id']]['custom_extension'] == 'kept'
    assert restored.resolve(a['id']).read_bytes() == original_a
    assert restored.resolve(b['id']).read_bytes() == original_b
    np.testing.assert_array_equal(restored.read_array(b['id']),image_b)
    assert restored.read_table(c['id'])['total_rows'] == 2
    restarted = ProjectStore(tmp_path/'second')
    assert restarted.get_project()['id'] == loaded['id']


def test_png_16bit_exact_values(tmp_path):
    source=np.array([[0,1,256],[257,40000,65535]],dtype=np.uint16)
    out=io.BytesIO()
    Image.fromarray(source).save(out,format='PNG')
    store=ProjectStore(tmp_path)
    asset=store.add_files([('sample.png',out.getvalue())])[0]
    assert asset['bit_depth']==16
    np.testing.assert_array_equal(store.read_array(asset['id']),source)


def test_revision_conflicts_missing_references_and_nonfinite(tmp_path):
    store=ProjectStore(tmp_path)
    first=store.get_project()
    second=store.get_project()
    first['title']='new'
    store.update_project(first)
    with pytest.raises(ConflictError):
        store.update_project(second)
    bad=store.get_project()
    bad['figure']['width_mm']=float('nan')
    with pytest.raises(ValueError):
        store.update_project(bad)
    bad=store.get_project()
    bad['panels']=[{'id':'x','asset_id':'a'*64,'x_mm':0,'y_mm':0,'width_mm':10,'height_mm':10}]
    with pytest.raises(ValueError,match='missing original'):
        store.update_project(bad)


def test_archive_rejects_traversal_hash_damage_and_missing_original(tmp_path):
    store=ProjectStore(tmp_path/'source')
    asset=store.add_files([('sample.tif',tiff_bytes(np.ones((16,16),np.uint16)))])[0]
    archive=store.export_archive()
    destination=ProjectStore(tmp_path/'destination')
    old_id=destination.get_project()['id']
    attack=modify_archive(archive,lambda files: files.update({'../escaped.txt':b'bad'}))
    with pytest.raises(ValueError,match='Unsafe'):
        destination.open_archive(attack)
    original_name='originals/'+asset['id']+'.tif'
    damaged=modify_archive(archive,lambda files: files.update({original_name:files[original_name]+b'x'}))
    with pytest.raises(ValueError,match='mismatch'):
        destination.open_archive(damaged)
    missing=modify_archive(archive,lambda files:files.pop(original_name))
    with pytest.raises(ValueError,match='missing'):
        destination.open_archive(missing)
    assert destination.get_project()['id']==old_id
    assert not (tmp_path/'escaped.txt').exists()


@pytest.mark.parametrize('name', ['data\\escape', 'data/escape\x00hidden'])
def test_archive_original_member_names_are_rejected_without_replacing_project(tmp_path, name):
    store = ProjectStore(tmp_path)
    before = store.get_project()
    attacked = modify_archive(store.export_archive(), lambda files: files.update({name: b'bad'}))
    with ZipFile(io.BytesIO(attacked)) as archive:
        assert name in [info.orig_filename for info in archive.infolist()]
    with pytest.raises(ValueError, match='Unsafe'):
        store.open_archive(attacked)
    assert store.get_project() == before


def test_zip_validator_rejects_windows_normalized_member_name_on_every_host():
    output = io.BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('data/escape', b'bad')
    with ZipFile(output) as archive:
        # This is ZipInfo's state after a raw backslash name is read on
        # Windows. Simulate only that state, not global os/path behaviour.
        archive.getinfo('data/escape').orig_filename = 'data\\escape'
        with pytest.raises(ValueError, match='Unsafe'):
            _validate_zip(archive)


def test_svg_rejects_active_external_and_entity_content():
    safe=b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    assert safe_svg(safe)==safe
    for unsafe in [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/x.png"/></svg>',
        b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com/x";</style></svg>',
    ]:
        with pytest.raises(ValueError):
            safe_svg(unsafe)


def test_lru_byte_bound_and_preview_parameter_identity(tmp_path):
    cache=ByteLRU(10,2)
    cache.put('a',b'123456')
    cache.put('b',b'789012')
    assert cache.get('a') is None and cache.bytes==6
    store=ProjectStore(tmp_path)
    asset=store.add_files([('sample.tif',tiff_bytes(np.arange(100,dtype=np.uint16).reshape(10,10)))])[0]
    first=store.preview(asset['id'],max_size=50,low=0,high=100)
    second=store.preview(asset['id'],max_size=50,low=0,high=100,invert=True)
    assert first!=second
    assert first==store.preview(asset['id'],max_size=50,low=0,high=100)


def wait_terminal(manager,jid):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        result=manager.get(jid)
        if result['status'] not in {'queued','running'}:
            return result
        time.sleep(.005)
    raise AssertionError('job did not finish')


def test_jobs_cancel_and_stale_results_never_commit(tmp_path):
    store=ProjectStore(tmp_path)
    started,release=threading.Event(),threading.Event()
    def runner(kind,params):
        started.set()
        assert release.wait(2)
        return {'kind':kind,'count':1}
    manager=JobManager(store,max_workers=1,runner=runner)
    try:
        job=manager.submit('microscopy',{})
        assert started.wait(1)
        draft=store.get_project(); draft['title']='edited during analysis'; store.update_project(draft)
        release.set()
        result=wait_terminal(manager,job['id'])
        assert result['status']=='done' and result['stale'] and not result['committed']
        assert not store.get_project()['measurements']
        started.clear();release.clear()
        job=manager.submit('microscopy',{})
        assert started.wait(1)
        manager.cancel(job['id']);release.set()
        assert wait_terminal(manager,job['id'])['status']=='cancelled'
        time.sleep(.02)
        assert not store.get_project()['measurements']
    finally:
        release.set();manager.shutdown()


def test_job_success_commits_auditable_result(tmp_path):
    store=ProjectStore(tmp_path)
    manager=JobManager(store,runner=lambda k,p:{'kind':k,'rows':[],'undefined':float('nan')})
    try:
        job=manager.submit('qpcr',{'rows':[]})
        done=wait_terminal(manager,job['id'])
        assert done['committed'] and done['result']['undefined'] is None
        project=store.get_project()
        assert project['revision']==1 and project['measurements'][0]['params']=={'rows':[]}
        assert project['audit'][-1]['action']=='analysis_completed'
    finally:
        manager.shutdown()


def test_invalid_manifest_shapes_are_rejected_without_replacing_project(tmp_path):
    store=ProjectStore(tmp_path)
    archive=store.export_archive()
    original_id=store.get_project()['id']
    for manifest in ([],{'format':'figwork','version':1,'assets':None},{'format':'figwork','version':1,'assets':[None]}):
        attacked=modify_archive(archive,lambda files:files.update({'manifest.json':json_bytes(manifest)}))
        with pytest.raises(ValueError):
            store.open_archive(attacked)
    assert store.get_project()['id']==original_id


def test_scene_ids_cannot_inject_dom_markup(tmp_path):
    store=ProjectStore(tmp_path)
    project=store.get_project()
    project['annotations']=[{'id':'bad"onclick="run','type':'text','x_mm':1,'y_mm':1,'text':'safe'}]
    with pytest.raises(ValueError):
        store.update_project(project)
    project=store.get_project()
    project['settings']['analysis_rois']=[{'id':'<svg id=attack>'}]
    with pytest.raises(ValueError):
        store.update_project(project)


def test_lzw_uint16_and_multichannel_tiff_keep_numeric_planes(tmp_path):
    store=ProjectStore(tmp_path)
    original=(np.arange(60*80,dtype=np.uint16).reshape(60,80)*11)
    compressed=io.BytesIO()
    tifffile.imwrite(compressed,original,compression='lzw',photometric='minisblack')
    stack=np.stack([original+i for i in range(5)])
    multi=io.BytesIO()
    tifffile.imwrite(multi,stack,photometric='minisblack',metadata={'axes':'CYX'})
    a,b=store.add_files([('compressed.tif',compressed.getvalue()),('channels.tif',multi.getvalue())])
    np.testing.assert_array_equal(store.read_array(a['id']),original)
    channels=store.read_array(b['id'])
    assert channels.shape==(60,80,5) and channels.dtype==np.uint16
    np.testing.assert_array_equal(channels[...,4],original+4)
    assert store.preview(b['id'],channel=4).startswith(b'\x89PNG')
    assert store.resolve(a['id']).read_bytes()==compressed.getvalue()

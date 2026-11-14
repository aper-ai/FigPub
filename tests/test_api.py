import io
import csv
import time

import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

from workbench.server import create_app


def image_bytes():
    out=io.BytesIO()
    tifffile.imwrite(out,np.arange(256*128,dtype=np.uint16).reshape(128,256),photometric='minisblack')
    return out.getvalue()


def test_api_import_preview_project_roundtrip_and_conflict(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/health').json()['ok']
        empty=client.get('/api/project').json()
        upload=client.post('/api/assets',files=[('files',('raw.tif',image_bytes(),'image/tiff'))])
        assert upload.status_code==200, upload.text
        asset=upload.json()['assets'][0]
        assert asset['bit_depth']==16
        assert client.get(asset['preview_url']).headers['content-type']=='image/png'
        assert client.get(f'/api/assets/{asset["id"]}/original').content==image_bytes()
        pixels=client.get(f'/api/assets/{asset["id"]}/pixels?max_size=64').json()
        assert pixels['width']==64 and len(pixels['data'])==pixels['width']*pixels['height']
        assert client.put('/api/project',json=empty).status_code==409
        current=client.get('/api/project').json()
        current['asset_edits']={asset['id']:{'custom':'preserved'}}
        saved=client.put('/api/project',json=current)
        assert saved.status_code==200 and saved.json()['asset_edits'][asset['id']]['custom']=='preserved'
        exported=client.post('/api/export',json={'format':'project'})
        assert exported.status_code==200
        client.post('/api/project/new',json={'title':'fresh'})
        restored=client.post('/api/project/open',files={'file':('example.figwork',exported.content,'application/zip')})
        assert restored.status_code==200,restored.text
        assert len(restored.json()['assets'])==1


def test_table_preview_and_csrf_guard(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response=client.post('/api/assets',files={'files':('table.csv',b'sample,ct\na,20\nb,22\n','text/csv')})
        asset=response.json()['assets'][0]
        table=client.get(f'/api/assets/{asset["id"]}/table?limit=1').json()
        assert table['total_rows']==2 and len(table['rows'])==1 and table['truncated']
        blocked=client.post('/api/project/new',json={},headers={'Origin':'https://untrusted.example'})
        assert blocked.status_code==403
        assert client.get('/api/health',headers={'Host':'evil.example'}).status_code==400


@pytest.mark.parametrize('delimiter,encoding', [(',', 'utf-8'), ('\t', 'utf-16'), (';', 'utf-8'), (',', 'gb18030')])
def test_txt_instrument_import_preview_and_real_qpcr_share_delimiter(tmp_path, delimiter, encoding):
    text = io.StringIO()
    text.write('Instrument export — SYNTHETIC\nRun metadata before the table\n')
    writer = csv.writer(text, delimiter=delimiter, lineterminator='\n')
    writer.writerow(['Sample Name', 'Target Name', 'Ct', 'Group', 'biological_replicate', 'technical_replicate'])
    for group, gene_ct in [('Control', 24), ('Treatment', 22)]:
        for repeat in (1, 2):
            sample = f'样本{group}{delimiter}\n{repeat}'
            for target, value in [('ACTB', 20), ('GeneX', gene_ct)]:
                writer.writerow([sample, target, value, group, sample, 1])
    payload = text.getvalue().encode(encoding)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/assets', files={'files': ('instrument.txt', payload, 'text/plain')})
        assert response.status_code == 200, response.text
        asset = response.json()['assets'][0]
        assert asset['kind'] == 'table' and asset['metadata']['header_row'] == 3
        assert asset['metadata']['delimiter'] == delimiter
        preview = client.get(f'/api/assets/{asset["id"]}/table').json()
        assert preview['columns'][0] == 'Sample Name' and len(preview['columns']) == 6
        assert preview['total_rows'] == 8 and preview['rows'][0]['Sample Name'] == f'样本Control{delimiter}\n1'
        canonical = client.get(f'/api/assets/{asset["id"]}/qpcr').json()
        assert canonical['header_row'] == 3 and canonical['rows'][0]['source_row'] == 4
        assert canonical['rows'][0]['sample'] == preview['rows'][0]['Sample Name']
        job = client.post('/api/jobs', json={'kind': 'qpcr', 'params': {
            'asset_id': asset['id'], 'reference_gene': 'ACTB', 'control_group': 'Control'}}).json()
        for _ in range(500):
            done = client.get('/api/jobs/' + job['id']).json()
            if done['status'] not in {'queued', 'running'}:
                break
            time.sleep(.005)
        assert done['status'] == 'done' and done['committed'], done
        treatment = next(group for group in done['result']['groups'] if group['group'] == 'Treatment')
        assert treatment['n_biological'] == 2 and treatment['mean'] == pytest.approx(4)
        assert client.get(f'/api/assets/{asset["id"]}/original').content == payload


def test_txt_rejects_single_column_and_respects_import_bounds(tmp_path, monkeypatch):
    import workbench.storage as storage
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/assets', files={'files': ('notes.txt', b'ordinary prose\nnot a table\n', 'text/plain')})
        assert response.status_code == 400
        assert not client.get('/api/project').json()['assets']
        monkeypatch.setattr(storage, 'MAX_TABLE_ROWS', 2)
        payload = b'sample,value\na,1\nb,2\nc,3\n'
        response = client.post('/api/assets', files={'files': ('oversize.txt', payload, 'text/plain')})
        assert response.status_code == 400 and 'limit' in response.text.lower()
        assert not client.get('/api/project').json()['assets']


def test_job_api_results_and_cancel(tmp_path):
    with TestClient(create_app(tmp_path,runner=lambda k,p:{'kind':k,'rows':[],'warnings':[]})) as client:
        response=client.post('/api/jobs',json={'kind':'qpcr','params':{'rows':[]}})
        assert response.status_code==200
        jid=response.json()['id']
        for _ in range(100):
            job=client.get('/api/jobs/'+jid).json()
            if job['status'] not in {'queued','running'}:
                break
            time.sleep(.005)
        assert job['status']=='done' and job['committed']
        assert client.get('/api/project').json()['measurements'][0]['kind']=='qpcr'
        assert client.get('/api/jobs/no-such-job').status_code==404


def test_demo_is_explicit_synthetic_and_multisource(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response=client.post('/api/demo')
        assert response.status_code==200,response.text
        project=response.json()
        assert project['settings']['synthetic_demo']
        assert len(project['assets'])==3 and len(project['panels'])==2
        assert all('DEMO_' in asset['name'] for asset in project['assets'])
        assert any('SYNTHETIC' in annotation['text'] for annotation in project['annotations'])


def test_real_qpcr_to_chart_and_portable_provenance(tmp_path):
    """Actual analysis -> SVG asset -> reopen retains original table and recipe."""
    with TestClient(create_app(tmp_path)) as client:
        project=client.post('/api/demo').json()
        table=next(asset for asset in project['assets'] if asset['kind']=='table')
        job=client.post('/api/jobs',json={'kind':'qpcr','params':{'asset_id':table['id'],'reference_gene':'ACTB','control_group':'Control','method':'delta_delta_ct'}}).json()
        for _ in range(500):
            result=client.get('/api/jobs/'+job['id']).json()
            if result['status'] not in {'queued','running'}:
                break
            time.sleep(.01)
        assert result['status']=='done',result
        assert len(result['result']['groups'])==2
        response=client.post('/api/charts',json={'measurement_id':job['id'],'chart_type':'bar'})
        assert response.status_code==200,response.text
        asset=response.json()['asset']
        assert asset['kind']=='svg'
        assert asset['metadata']['analysis_source']['source_asset_ids']==[table['id']]
        assert client.get(asset['preview_url']).status_code==200
        archive=client.post('/api/export',json={'format':'project'})
        loaded=client.post('/api/project/open',files={'file':('measured.figwork',archive.content)})
        assert loaded.status_code==200,loaded.text
        chart=next(a for a in loaded.json()['assets'] if a['id']==asset['id'])
        assert chart['metadata']['recipe_sha256']==asset['metadata']['recipe_sha256']
        assert len(loaded.json()['measurements'])==1


def test_readonly_raw_wb_and_nuclei_jobs_and_errors(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        project=client.post('/api/demo').json()
        gel=next(asset for asset in project['assets'] if 'gel_' in asset['name'])
        nuclei=next(asset for asset in project['assets'] if 'nuclei' in asset['name'])
        rois=[]
        for index,cx in enumerate([170,270,370]):
            for role,cy in [('target',105+3*(index+1)),('control',255)]:
                rois.append({'id':f'{role}_{index}','asset_id':gel['id'],'x':cx-27,'y':cy-15,'width':54,'height':30,
                             'sample_id':f's{index}','group':'Group','biological_replicate':f'b{index}',
                             'technical_replicate':'1','exposure_id':'1','role':role,'protein':'Target' if role=='target' else 'Actin'})
        cases=[('wb',{'rois':rois,'background':'border','polarity':'dark'}),
               ('microscopy',{'asset_id':nuclei['id'],'method':'nuclei','min_area':30,'um_per_px':.5})]
        for kind,params in cases:
            start=client.post('/api/jobs',json={'kind':kind,'params':params})
            assert start.status_code==200,start.text
            jid=start.json()['id']
            for _ in range(500):
                result=client.get('/api/jobs/'+jid).json()
                if result['status'] not in {'queued','running'}:
                    break
                time.sleep(.01)
            assert result['status']=='done',result
            if kind=='wb':
                assert len(result['result']['rows'])==6
                assert result['result']['groups'][0]['n_biological']==3
            else:
                assert result['result']['count']==9
            chart=client.post('/api/charts',json={'measurement_id':jid,'chart_type':'scatter'})
            assert chart.status_code==200,chart.text
        assert client.post('/api/jobs',json={'kind':'wb','params':{'rois':[None]}}).status_code==400
        assert client.post('/api/jobs',json={'kind':'qpcr','params':{'rows':'invalid'}}).status_code==400
        for name,data in [('invalid.xlsx',b'badzip'),('invalid.tif',b'badtiff'),('invalid.pdf',b'badpdf')]:
            assert client.post('/api/assets',files={'files':(name,data)}).status_code==400


def test_static_font_routes_and_self_exported_svg_reimport(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/').status_code==200
        assert client.get('/app.js').status_code==200
        assert client.get('/gel-worker.js').status_code==200
        assert client.get('/fonts/DejaVuSans.ttf').status_code==200
        assert client.get('/fonts/not-allowed.ttf').status_code==404
        project=client.post('/api/demo').json()
        exported=client.post('/api/export',json={'format':'svg','dpi':300})
        assert exported.status_code==200,exported.text[:500]
        assert b'@font-face' in exported.content
        imported=client.post('/api/assets',files={'files':('self_export.svg',exported.content,'image/svg+xml')})
        assert imported.status_code==200,imported.text
        asset=imported.json()['assets'][0]
        assert client.get(asset['preview_url']).status_code==200


def test_legacy_gelfigure_and_sciefig_upload_api(tmp_path):
    import runpy
    from pathlib import Path
    fixtures=runpy.run_path(str(Path(__file__).with_name('test_migration.py')))
    with TestClient(create_app(tmp_path)) as client:
        for filename,builder,count in [('legacy.gelfig',fixtures['gel_bytes'],1),('legacy.sfig',fixtures['sfig_bytes'],2)]:
            response=client.post('/api/project/open',files={'file':(filename,builder(),'application/octet-stream')})
            assert response.status_code==200,response.text
            project=response.json()
            assert len(project['assets'])==count
            assert project['settings']['migration_warnings']
            assert project['settings']['migration_source_format']=='.'+filename.split('.')[-1]
            assert all('archive_names' in a['metadata']['legacy'] for a in project['assets'])
            for asset in project['assets']:
                assert client.get(asset['preview_url']).status_code==200
            exported=client.post('/api/export',json={'format':'project'})
            reopened=client.post('/api/project/open',files={'file':('migrated.figwork',exported.content,'application/zip')})
            assert reopened.status_code==200,reopened.text
            assert len(reopened.json()['assets'])==count
        unchanged=client.get('/api/project').json()['id']
        response=client.post('/api/project/open',files={'file':('bad.sfig',b'corrupt')})
        assert response.status_code==400
        assert client.get('/api/project').json()['id']==unchanged


def test_binary_localization_matches_json_without_precision_loss(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        upload=client.post('/api/assets',files={'files':('raw.tif',image_bytes(),'image/tiff')}).json()
        asset=upload['assets'][0]
        endpoint='/api/assets/'+asset['id']
        old=client.get(endpoint+'/pixels?max_size=64').json()
        new=client.get(endpoint+'/pixels.bin?max_size=64')
        assert new.status_code==200
        assert new.headers['x-pixel-format']=='float32-le'
        assert int(new.headers['x-image-width'])==old['width']
        assert int(new.headers['x-image-height'])==old['height']
        assert int(new.headers['x-source-width'])==old['source_width']
        assert int(new.headers['x-bit-depth'])==16
        binary=np.frombuffer(new.content,dtype='<f4')
        assert len(new.content)==4*old['width']*old['height']
        np.testing.assert_array_equal(binary,np.asarray(old['data'],dtype=np.float32))
        assert len(new.content)<len(client.get(endpoint+'/pixels?max_size=64').content)

"""Measure local API serialization, not internet speed or scientific accuracy."""
from io import BytesIO
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import tifffile
from fastapi.testclient import TestClient
from workbench.server import create_app


def main():
    with TemporaryDirectory() as directory:
        array=(np.arange(4096*4096,dtype=np.uint32)%65536).astype(np.uint16).reshape(4096,4096)
        buffer=BytesIO();tifffile.imwrite(buffer,array,photometric='minisblack')
        with TestClient(create_app(directory)) as client:
            response=client.post('/api/assets',files=[('files',('synthetic.tif',buffer.getvalue(),'image/tiff'))])
            response.raise_for_status();asset_id=response.json()['assets'][0]['id']
            output={}
            responses={}
            for label,suffix in [('binary','pixels.bin'),('json','pixels')]:
                path=f'/api/assets/{asset_id}/{suffix}?max_size=1024'
                tick=perf_counter();warm=client.get(path);warm.raise_for_status();cold=(perf_counter()-tick)*1000
                samples=[]
                for _ in range(5):
                    tick=perf_counter();result=client.get(path);result.raise_for_status();samples.append((perf_counter()-tick)*1000)
                responses[label]=result
                output[label]={'cold_ms':cold,'samples_ms':samples,'median_ms':float(np.median(samples)),
                               'p95_ms':float(np.percentile(samples,95)),'response_bytes':len(result.content),
                               'content_type':result.headers['content-type']}
            binary=np.frombuffer(responses['binary'].content,dtype='<f4')
            json_values=np.asarray(responses['json'].json()['data'],dtype=np.float32)
            np.testing.assert_array_equal(binary,json_values)
            report={'benchmark_version':1,'synthetic_data':True,'python':platform.python_version(),
                    'scope':'FastAPI in-process TestClient, cached 1024x1024 localization view of a 4096x4096 uint16 source; includes response serialization, excludes network/worker calculation.',
                    'same_values':True,'cases':output,
                    'limitations':['Not a throughput or latency claim for all machines.','These pixels are for localization only; analysis uses the original uint16 image.','Not an upstream application comparison.']}
            destination=Path(__file__).with_name('results_transport.json')
            destination.write_text(json.dumps(report,indent=2),'utf-8');print(json.dumps(report,indent=2))


if __name__=='__main__':main()

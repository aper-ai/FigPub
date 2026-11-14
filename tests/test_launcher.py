"""The normal launcher must not create a second writer for one working folder."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request


def test_loopback_launcher_single_writer_and_port_conflict(tmp_path):
    from run import reserve_port
    reserved=reserve_port(0)
    occupied=reserved.getsockname()[1]
    root=Path(__file__).resolve().parents[1]
    command=[sys.executable,str(root/'run.py'),'--no-browser','--data-dir',str(tmp_path),'--port',str(occupied)]
    process=subprocess.Popen(command,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        instance=tmp_path/'instance.json'
        deadline=time.monotonic()+15
        project=None
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            if instance.is_file():
                port=json.loads(instance.read_text('utf-8'))['port']
                assert port!=occupied
                base=f'http://127.0.0.1:{port}'
                try:
                    with urllib.request.urlopen(base+'/api/project',timeout=.5) as response:
                        project=json.load(response)
                    break
                except OSError:pass
            time.sleep(.04)
        assert project is not None,'launcher did not become ready'
        project['title']='single-writer-sentinel'
        request=urllib.request.Request(base+'/api/project',data=json.dumps(project).encode(),
                                       headers={'Content-Type':'application/json'},method='PUT')
        with urllib.request.urlopen(request,timeout=3) as response:updated=json.load(response)
        second=subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=10)
        assert second.returncode==0,second.stderr
        assert 'already open' in second.stdout
        with urllib.request.urlopen(base+'/api/project',timeout=3) as response:current=json.load(response)
        assert current['id']==updated['id'] and current['title']=='single-writer-sentinel'
        assert current['revision']==updated['revision']
        with urllib.request.urlopen(base+'/api/health',timeout=3) as response:health=json.load(response)
        assert health['application']=='figure-workbench'
        assert health['instance_id']==json.loads(instance.read_text('utf-8'))['instance_id']
        assert list((tmp_path/'logs').glob('launcher-*.log'))
    finally:
        if os.name == 'nt':
            process.terminate()
        else:
            process.send_signal(signal.SIGINT)
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        reserved.close()
    if os.name != 'nt':
        assert process.returncode == 0
        assert not (tmp_path/'instance.json').exists()
        from run import InstanceLock
        released=InstanceLock(tmp_path)
        try:assert released.acquire()
        finally:released.close()


def test_startup_failure_releases_port_lock_and_leaves_diagnostic_log(tmp_path, monkeypatch):
    import run
    from workbench import server
    bound=[]
    original=run.reserve_port
    def remember_port(port):
        listener=original(port)
        bound.append(listener)
        return listener
    def broken_application(*args, **kwargs):
        raise RuntimeError('intentional-launch-regression')
    monkeypatch.setattr(run,'reserve_port',remember_port)
    monkeypatch.setattr(server,'create_app',broken_application)
    monkeypatch.setenv('FIGURE_WORKBENCH_DATA',str(tmp_path))
    assert run.main(['--no-browser','--data-dir',str(tmp_path)])==1
    assert bound[0].fileno()==-1
    lock=run.InstanceLock(tmp_path)
    try:assert lock.acquire()
    finally:lock.close()
    assert not (tmp_path/'instance.json').exists()
    assert not (tmp_path/'current.json').exists()
    log=next((tmp_path/'logs').glob('launcher-*.log')).read_text('utf-8')
    assert 'intentional-launch-regression' in log and 'Traceback' in log


def test_windowless_failure_shows_message_and_keeps_writable_streams(tmp_path, monkeypatch):
    import run
    from workbench import runtime_paths
    messages=[]
    def fail_resources():raise FileNotFoundError('missing-bundled-font')
    monkeypatch.setattr(runtime_paths,'check_resources',fail_resources)
    monkeypatch.setattr(run,'_native_message',lambda title,message,**options: messages.append((title,message,options)) or True)
    monkeypatch.setattr(sys,'stdout',None)
    monkeypatch.setattr(sys,'stderr',None)
    monkeypatch.setenv('FIGURE_WORKBENCH_DATA',str(tmp_path))
    result=run.main(['--no-browser','--data-dir',str(tmp_path)])
    assert result==1
    assert messages and messages[-1][2]['error'] is True
    assert 'missing-bundled-font' in messages[-1][1]
    assert sys.stderr is not None and not sys.stderr.isatty()
    sys.stderr.write('post-failure-stream-is-usable\n')
    log=next((tmp_path/'logs').glob('launcher-*.log')).read_text('utf-8')
    assert 'missing-bundled-font' in log and 'post-failure-stream-is-usable' in log


def test_existing_instance_rejects_unrelated_loopback_service(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import run
    answer={'ok':True,'application':'figure-workbench','instance_id':'b'*32}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data=json.dumps(answer).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    identity={'application':'figure-workbench','instance_id':'a'*32,'port':server.server_port,'pid':1}
    run._write_instance(tmp_path/'instance.json',identity)
    try:
        assert run._existing_url(tmp_path,timeout=.12) is None
        answer['instance_id']='a'*32
        assert run._existing_url(tmp_path,timeout=.5)==f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_instance_metadata_is_complete_and_temporary_files_removed(tmp_path):
    import run
    first={'application':'figure-workbench','instance_id':'a'*32,'port':1111}
    second={'application':'figure-workbench','instance_id':'b'*32,'port':2222}
    path=tmp_path/'instance.json'
    run._write_instance(path,first)
    run._write_instance(path,second)
    assert json.loads(path.read_text('utf-8'))==second
    assert list(tmp_path.glob('.instance-*'))==[]

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ptagent.bridge import local_bridge_env


def test_loopback_bypass_retains_external_proxy_and_existing_exclusions():
    original={"HTTP_PROXY":"http://proxy.example:8080","NO_PROXY":"example.org","no_proxy":"intranet"}
    result=local_bridge_env(original)
    assert result["HTTP_PROXY"]==original["HTTP_PROXY"]
    assert result["NO_PROXY"]==result["no_proxy"]
    assert set(result["NO_PROXY"].split(","))=={"example.org","intranet","127.0.0.1","localhost","::1"}
    assert "127.0.0.1" not in original["NO_PROXY"]


def test_real_loopback_request_bypasses_unreachable_proxy():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"local-bridge-ok")
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    env=local_bridge_env({**os.environ,"HTTP_PROXY":"http://127.0.0.1:1","http_proxy":"http://127.0.0.1:1","NO_PROXY":"","no_proxy":""})
    try:
        code="import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:%d/ping',timeout=3).read().decode())" % server.server_port
        result=subprocess.run([sys.executable,"-c",code],env=env,capture_output=True,text=True,timeout=8)
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()=="local-bridge-ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

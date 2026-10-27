"""Local persistent RF advice server. Never contacts Kubernetes or trains models.

python -m gpu_lab.runtime.http_server --port 18081
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from .bundle import DEFAULT_BUNDLE
from .service import SuitabilityService
from .volcano import VolcanoRFAdapter, RF_SCHEMA

MAX_BODY = 1024*1024


def make_server(service,host="127.0.0.1",port=18081):
    if host not in ("127.0.0.1","localhost","::1"):raise ValueError("MVP only binds a loopback interface")
    adapter=VolcanoRFAdapter(service)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,body):
            raw=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length",str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        def do_GET(self):
            if self.path!="/health":return self.reply(404,{"status":"not_found"})
            self.reply(200,{"schema_version":RF_SCHEMA,"model_version":service.version,"model_ready":service.model is not None,
                            "load_error":service.model_error,"logical_allocation_only":True,"supervision":"synthetic"})
        def do_POST(self):
            if self.path!="/v1/rank":return self.reply(404,{"status":"not_found"})
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 0 < length <= MAX_BODY:return self.reply(413,{"status":"invalid_input","errors":["body_size"]})
                self.connection.settimeout(3)
                raw=self.rfile.read(length)
                if len(raw)!=length:raise ValueError("incomplete_body")
                data=json.loads(raw,parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite_json")))
                if not isinstance(data,dict):raise ValueError("body_must_be_object")
                result=adapter.rank(data)
                self.reply(400 if result["status"]=="invalid_input" else 200,result)
            except (ValueError,TypeError,UnicodeError,TimeoutError) as exc:
                self.reply(400,{"status":"invalid_input","errors":[str(exc)]})
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    return server


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--host',default='127.0.0.1');ap.add_argument('--port',type=int,default=18081)
    ap.add_argument('--bundle',type=Path,default=DEFAULT_BUNDLE);a=ap.parse_args()
    service=SuitabilityService(a.bundle);server=make_server(service,a.host,a.port)
    print(json.dumps(dict(listening=server.server_address,model_version=service.version,model_ready=service.model is not None,logical_allocation_only=True)),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()

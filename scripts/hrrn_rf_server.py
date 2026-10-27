"""Local WSL bridge for native Go tests; immutable model, no cluster client."""
import argparse
import json
import sys
import threading
import socket
import socketserver
import select
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gpu_lab.runtime.http_server import make_server
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.runtime.audit_server import audit_record

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18084)
    p.add_argument('--bridge-host',required=True);p.add_argument('--allowed-client',required=True);a=p.parse_args()
    service=SuitabilityService();assert service.model is not None and not service.model_error
    lock=threading.Lock()
    def observe(request,response):
        with lock:print(json.dumps(audit_record(request,response),ensure_ascii=False,allow_nan=False),flush=True)
    # Keep the frozen inference service on loopback. This test-only transport
    # accepts exactly the local WSL guest address on the private WSL adapter.
    server=make_server(service,host='127.0.0.1',port=a.port,observer=observe)
    print(json.dumps(dict(event='loaded',model=service.version,listen=server.server_address)),flush=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            if self.client_address[0]!=a.allowed_client:return
            with socket.create_connection(('127.0.0.1',a.port),timeout=3) as target:
                while True:
                    readable,_,_=select.select([self.request,target],[],[],5)
                    if not readable:return
                    for source in readable:
                        data=source.recv(65536)
                        if not data:return
                        (target if source is self.request else self.request).sendall(data)
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address=True
        daemon_threads=True
    with Server((a.bridge_host,18083),Relay) as relay:relay.serve_forever()

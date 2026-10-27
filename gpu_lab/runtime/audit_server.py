"""Loopback sidecar entrypoint: load frozen RF once, emit advice audit records.

Never initializes a Kubernetes client or reserves resources. Shares a Pod network
namespace with the independent scheduler; no externally exposed inference port.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import threading

from .bundle import DEFAULT_BUNDLE
from .http_server import make_server
from .service import SuitabilityService


def audit_record(request,response):
    pod=request.get('pod',{});m=pod.get('metadata',{})
    return {'event':'rf_advice','utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'request_id':response.get('request_id'),'snapshot_token':response.get('snapshot_token'),
            'model_version':response.get('model_version'),'rf_prediction_enabled':response.get('rf_prediction_enabled'),
            'status':response.get('status'),'model_ready':response.get('model_ready'),
            'pod':{k:m.get(k) for k in ('name','namespace','uid','labels')},
            'requests':[{'name':c['name'],'resources':c.get('resources',{})} for c in pod.get('spec',{}).get('containers',[])],
            'gpus':request.get('gpus',[]),'classifications':response.get('classifications',[]),
            'assignments':response.get('assignments',[]),'node_score':response.get('score'),
            'errors':response.get('errors',[]),'source':'RF request snapshot; native scheduler ledger remains authoritative',
            'real_performance_guarantee':False}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--port',type=int,default=18081)
    ap.add_argument('--bundle',type=Path,default=DEFAULT_BUNDLE);a=ap.parse_args()
    service=SuitabilityService(a.bundle)
    if service.model is None or service.model_error:
        print(json.dumps({'event':'model_load_failed','error':service.model_error}),flush=True)
        raise SystemExit(2)
    lock=threading.Lock()
    def observe(request,response):
        # Logging failures must not corrupt a valid advice response.
        try:
            record=json.dumps(audit_record(request,response),ensure_ascii=False,allow_nan=False)
            with lock:print(record,flush=True)
        except (OSError,ValueError,TypeError) as exc:
            print('RF audit unavailable: '+type(exc).__name__,file=sys.stderr,flush=True)
    server=make_server(service,port=a.port,observer=observe)
    print(json.dumps({'event':'model_loaded','model_version':service.version,'model_load_count':1,'listen':server.server_address}),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()

"""CLI/JSON-lines worker. No HTTP listener, GPU workload or cluster client."""
import argparse
import json
from pathlib import Path
import sys

from .adapters import dispatch
from .bundle import DEFAULT_BUNDLE, freeze
from .service import SuitabilityService


def main():
    # JSON transport is UTF-8 on every host, including Windows code-page defaults.
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("freeze","classify","rank","json-lines","demo"))
    parser.add_argument("--bundle",type=Path,default=DEFAULT_BUNDLE)
    parser.add_argument("--gpu-json",type=Path)
    parser.add_argument("--gpus-json",type=Path)
    parser.add_argument("--pod",type=Path)
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze(args.bundle)
    elif args.command == "demo":
        from .run import run
        result = run(args.output,args.bundle)
    else:
        service = SuitabilityService(args.bundle)
        if args.command == "classify":
            if not args.gpu_json:
                parser.error("--gpu-json required")
            result = service.classify_gpu(json.loads(args.gpu_json.read_text(encoding="utf-8")))
        elif args.command == "rank":
            if not args.pod or not args.gpus_json:
                parser.error("--pod and --gpus-json required")
            result = service.rank_gpus(args.pod,json.loads(args.gpus_json.read_text(encoding="utf-8")))
        else:
            # Load joblib once, process arbitrarily many messages in one process.
            for line in sys.stdin:
                if not line.strip():
                    continue
                try:
                    reply = dispatch(service,json.loads(line))
                except (ValueError,TypeError) as exc:
                    reply = {"status":"invalid_input","errors":[str(exc)]}
                print(json.dumps(reply,ensure_ascii=False,allow_nan=False),flush=True)
            return
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__ == "__main__":
    main()

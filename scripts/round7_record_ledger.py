"""Convert saved kubectl GET JSON into UID ledgers; never calls Kubernetes."""
import argparse
import json
from pathlib import Path


def records(objects,guard=False):
    result=[]
    for obj in objects:
        m=obj['metadata']
        if m.get('labels',{}).get('gpu-lab/experiment')!='round7-rf' or not m.get('uid'):
            raise ValueError('Only actual UID-bearing experiment objects may enter rollback ledger')
        kind=obj['kind'] if guard else obj['kind'].lower()
        result.append({'kind':kind,'name':m['name'],'namespace':m.get('namespace'),'uid':m['uid']})
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--input',nargs='+',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--guard',action='store_true');a=ap.parse_args()
    objects=[]
    for path in a.input:
        value=json.loads(path.read_text(encoding='utf-8'));objects.extend(value['items'] if 'items' in value else [value])
    if a.output.exists():raise SystemExit('Do not overwrite an existing run ledger')
    a.output.write_text(json.dumps(records(objects,a.guard),indent=2),encoding='utf-8')


if __name__=='__main__':main()

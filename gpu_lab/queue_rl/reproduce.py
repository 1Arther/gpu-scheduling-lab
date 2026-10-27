"""One non-selection rerun verifies calendar optimization preserves training."""
import argparse,shutil
from pathlib import Path
from .experiment import DEFAULT,read,dump

def reproduce(out=DEFAULT):
    import torch
    from sb3_contrib import MaskablePPO
    from .train import train
    torch.set_num_threads(1);out=Path(out);target=out/'reproducibility'
    target.mkdir(exist_ok=True)
    (target/'inputs').mkdir(exist_ok=True)
    shutil.copyfile(out/'inputs/train.json',target/'inputs/train.json')
    shutil.copyfile(out/'scales.json',target/'scales.json')
    mid='joint-idle0.25-seed17'
    train(target,'joint',.25,17,512)
    a=MaskablePPO.load(out/'training'/mid/'model.zip',device='cpu').policy.state_dict()
    b=MaskablePPO.load(target/'training'/mid/'model.zip',device='cpu').policy.state_dict()
    equal=all(torch.equal(a[k],b[k]) for k in a)
    meta=read(target/'training'/mid/'metadata.json')
    dump(out/'training-reproducibility.json',dict(bit_exact_policy_tensors=equal,model_id=mid,
        rerun_steps=meta['steps'],rerun_process_seconds=meta['wall_seconds'],
        scope='Engineering rerun only; not a candidate for development selection',
        reason='Calendar query optimized after training; verify identical policy tensors with current source'))
    assert equal,'Training changed after calendar optimization'
    print(read(out/'training-reproducibility.json'))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=DEFAULT);a=p.parse_args();reproduce(a.out)

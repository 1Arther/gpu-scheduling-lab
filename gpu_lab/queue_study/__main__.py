import argparse
from . import experiments as e


def main():
    p=argparse.ArgumentParser(description='Synthetic CPU-only HRRN stage-2 study: develop, freeze, test, analyze, replay')
    p.add_argument('stage',choices=('develop','freeze','test','analyze','replay','profile'))
    p.add_argument('--output',required=True)
    p.add_argument('--development')
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--seed',type=int,default=900001)
    p.add_argument('--condition',default='high_load__independent__under')
    p.add_argument('--policy',choices=e.POLICIES,default='hrrn_easy')
    args=p.parse_args()
    if args.workers<1:p.error('--workers must be positive')
    if args.stage=='develop':e.develop(args.output,args.workers)
    elif args.stage=='profile':e.profile(args.output)
    elif args.stage=='freeze':
        if not args.development:p.error('--development is required for freeze')
        e.freeze(args.output,args.development)
    elif args.stage=='test':e.test(args.output,args.workers)
    elif args.stage=='replay':e.replay(args.output,args.seed,args.condition,args.policy)
    elif args.stage=='analyze':
        from .analysis import analyze
        if not args.development:p.error('--development is required for analysis')
        analyze(args.output,args.development)


if __name__=='__main__':main()

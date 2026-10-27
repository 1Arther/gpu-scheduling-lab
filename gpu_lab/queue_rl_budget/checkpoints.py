"""Separate actor and critic changes; nonzero total change alone is insufficient."""
import torch
from sb3_contrib import MaskablePPO
from .common import OUT,SEEDS,read,dump,csv_write
from .evaluate import model_path

def run():
    torch.set_num_threads(1);rows=[]
    for seed in SEEDS:
        previous=None;previous_budget=None
        for budget in (0,512,2048,8192,32768):
            path=model_path(seed,budget)
            if not path.exists():continue
            model=MaskablePPO.load(path,device='cpu');state=model.policy.state_dict()
            if previous is not None:
                names=dict(actor=[k for k in state if 'policy_net' in k or k.startswith('action_net')],
                           critic=[k for k in state if 'value_net' in k])
                values={group:float(torch.linalg.vector_norm(torch.cat([(state[k]-previous[k]).reshape(-1) for k in keys]))) for group,keys in names.items()}
                rows.append(dict(seed=seed,from_budget=previous_budget,to_budget=budget,actor_delta_norm=values['actor'],critic_delta_norm=values['critic'],
                    trained_epochs=model._n_updates,finite=all(torch.isfinite(x).all().item() for x in state.values())))
            previous=state;previous_budget=budget
    csv_write(OUT/'analysis/checkpoint-actor-critic.csv',rows);dump(OUT/'analysis/checkpoint-actor-critic.json',rows)

if __name__=='__main__':run()

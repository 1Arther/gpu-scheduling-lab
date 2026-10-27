"""Pre-frozen analysis: paired seed blocks, declared metrics, static figures."""
import argparse,csv
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from .experiment import DEFAULT,read,dump,digest,verify_freeze,preserved_files

LABELS={'fifo':'FIFO','sjf':'SJF','hrrn':'HRRN','hrrn_age':'Age','hrrn_easy':'EASY',
    'hrrn_protect':'Fixed vGPU','hrrn_whole_gpu':'Whole GPU','hrrn_adaptive_heuristic':'Heuristic',
    'hrrn_ppo':'PPO joint','ppo_switch_only':'PPO switch','ppo_no_hrrn':'PPO no HRRN'}
METRICS=['mean_wait','p95_wait','p99_wait','large_p95_wait','large_p99_wait','multi_gpu_p95_wait',
    'multi_gpu_p99_wait','small_p95_wait','small_p99_wait','long_wait_ratio','mean_bounded_slowdown',
    'completed_count','completed_work','logical_allocation_rate_compute_units',
    'logical_allocation_rate_memory_mib','logical_allocation_rate_instance_slots',
    'protected_idle_capacity_time_compute_units','protected_idle_capacity_time_memory_mib','protected_idle_capacity_time_instance_slots',
    'large_mean_wait','small_mean_wait','small_large_mean_wait_gap','activation_count','activation_mean_predicted_delay',
    'activation_mean_observed_delay','decision_ms_mean','dispatch_total_ms_mean','inference_ms_mean','protect_action_ratio']

def csv_write(path,rows):
    if not rows:return
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(rows)

def block_values(rows,policy,metric):
    grouped=defaultdict(list)
    for r in rows:
        v=r['metrics'].get(metric)
        if r['policy']==policy and v is not None:grouped[r['seed']].append(v)
    return {s:float(np.mean(v)) for s,v in grouped.items()}

def analyze(out=DEFAULT):
    out=Path(out);verify_freeze(out);rows=read(out/'test/results.json');dest=out/'analysis';dest.mkdir(exist_ok=True)
    rng=np.random.default_rng(76291);resample=rng.integers(0,20,size=(100000,20))
    summaries=[]
    for policy in LABELS:
        entry=dict(policy=policy,label=LABELS[policy])
        for metric in METRICS:
            values=block_values(rows,policy,metric);x=np.array([values[s] for s in sorted(values)])
            if not len(x):entry[metric]=None;continue
            entry[metric]=float(np.mean(x));entry[metric+'_sd']=float(np.std(x,ddof=1)) if len(x)>1 else 0.
            entry[metric+'_min']=float(np.min(x));entry[metric+'_max']=float(np.max(x))
            if len(x)==20:
                boots=x[resample].mean(1);entry[metric+'_ci95_low'],entry[metric+'_ci95_high']=map(float,np.quantile(boots,[.025,.975]))
        summaries.append(entry)
    comparisons=[]
    for reference in ('hrrn','hrrn_protect','hrrn_adaptive_heuristic','hrrn_easy'):
        for metric in METRICS:
            a=block_values(rows,'hrrn_ppo',metric);b=block_values(rows,reference,metric);seeds=sorted(a.keys()&b.keys())
            if not seeds:continue
            x=np.array([a[s]-b[s] for s in seeds]);primary=metric in ('p95_wait','long_wait_ratio','completed_work')
            idx=resample if len(x)==20 else np.random.default_rng(76291).integers(0,len(x),size=(100000,len(x)))
            boot=x[idx].mean(1);lo,hi=np.quantile(boot,[.025,.975]);alpha=.05/12 if primary else .05
            clo,chi=np.quantile(boot,[alpha/2,1-alpha/2])
            comparisons.append(dict(reference=reference,metric=metric,mean_difference=float(x.mean()),
                ci95_low=float(lo),ci95_high=float(hi),controlled_low=float(clo),controlled_high=float(chi),
                primary=primary,paired_seed_blocks=len(seeds),sd_difference=float(np.std(x,ddof=1)),
                difference_definition='PPO minus comparator; lower waiting/cost better; higher work better'))
    conditions=[];actions=[]
    for condition in sorted({r['condition'] for r in rows}):
        for policy in LABELS:
            rr=[r for r in rows if r['condition']==condition and r['policy']==policy]
            e=dict(condition=condition,policy=policy)
            for metric in METRICS:
                vv=[r['metrics'].get(metric) for r in rr if r['metrics'].get(metric) is not None]
                e[metric]=float(np.mean(vv)) if vv else None
            conditions.append(e)
            allactions=[d for r in rr for d in r['actions']]
            counts=Counter(d['action'] for d in allactions);reasons=Counter(d['reason'] for d in allactions)
            actions.append(dict(condition=condition,policy=policy,decisions=len(allactions),**{f'action_{i}':counts[i] for i in range(4)},reasons=dict(reasons)))
    dump(dest/'summary.json',summaries);csv_write(dest/'summary.csv',summaries)
    dump(dest/'paired.json',comparisons);csv_write(dest/'paired.csv',comparisons)
    dump(dest/'conditions.json',conditions);csv_write(dest/'conditions.csv',conditions);dump(dest/'actions.json',actions)
    indexed={(r['seed'],r['condition'],r['policy']):r for r in rows};effects=[]
    for r in rows:
        if not r['actions']:continue
        baseline=indexed[(r['seed'],r['condition'],'hrrn')]
        starts={e['job_id']:e['time'] for e in r['physical_events'] if e['kind']=='start'}
        bst={e['job_id']:e['time'] for e in baseline['physical_events'] if e['kind']=='start'}
        for d in r['actions']:
            if not d['action']:continue
            jid=d['context']['owner']['job_id'];now=d['context']['time']
            effects.append(dict(seed=r['seed'],condition=r['condition'],policy=r['policy'],job_id=jid,
                activation_time=now,predicted_delay=d['context']['combos'][d['action']-1]['delay'],
                observed_delay=starts[jid]-now,wait_difference_vs_no_protection=starts[jid]-bst[jid]))
    csv_write(dest/'protection-effects.csv',effects);dump(dest/'protection-effects.json',effects)
    stress=read(out/'development/scaling-stress.json');stress_summary=[]
    for condition in sorted({r['condition'] for r in stress}):
        for policy in ('hrrn','hrrn_easy','hrrn_adaptive_heuristic','hrrn_ppo'):
            rr=[r for r in stress if r['condition']==condition and r['policy']==policy]
            stress_summary.append(dict(condition=condition,policy=policy,**{k:float(np.mean([r['metrics'][k] for r in rr])) for k in ('p95_wait','mean_wait','completed_work','protect_action_ratio')}))
    csv_write(dest/'scaling-stress.csv',stress_summary)
    csv_write(dest/'per-trace-metrics.csv',[dict(seed=r['seed'],condition=r['condition'],policy=r['policy'],**r['metrics']) for r in rows])
    selection=read(out/'selection.json');dev=read(out/'development/baselines.json')
    dev+=read(out/f"development/heuristic-{selection['heuristic']['id']}.json")
    for variant,m in selection['models'].items():
        rr=read(out/f"development/{m['id']}.json")
        for r in rr:r['policy']='hrrn_ppo' if variant=='joint' else 'ppo_'+variant
        dev+=rr
    dev_summary=[]
    for policy in LABELS:
        rr=[r for r in dev if r['policy']==policy]
        e=dict(policy=policy,traces=len(rr))
        for k in METRICS:
            v=[r['metrics'][k] for r in rr if r['metrics'].get(k) is not None]
            e[k]=float(np.mean(v)) if v else None
        dev_summary.append(e)
    csv_write(dest/'development-summary.csv',dev_summary);dump(dest/'development-summary.json',dev_summary)
    # Primary 9-method comparison plus equally-budgeted ablations in separate panel.
    bypolicy={r['policy']:r for r in summaries};main=list(LABELS)[:9]
    def figure(name,metrics,title,policies=main):
        fig,axs=plt.subplots(1,len(metrics),figsize=(5*len(metrics),4.8),squeeze=False)
        for ax,metric in zip(axs[0],metrics):
            y=[bypolicy[p].get(metric) or 0 for p in policies];x=np.arange(len(y))
            ax.bar(x,y,color=['#277da8' if p!='hrrn_ppo' else '#e17735' for p in policies])
            low=[max(0,y[i]-(bypolicy[p].get(metric+'_ci95_low') or y[i])) for i,p in enumerate(policies)]
            high=[max(0,(bypolicy[p].get(metric+'_ci95_high') or y[i])-y[i]) for i,p in enumerate(policies)]
            ax.errorbar(x,y,yerr=[low,high],fmt='none',color='black',capsize=2,lw=.8)
            ax.set_xticks(x,[LABELS[p] for p in policies],rotation=65,ha='right',fontsize=8)
            ax.set_title(metric.replace('_',' '),fontsize=10);ax.grid(axis='y',alpha=.2)
        fig.suptitle(title);fig.tight_layout();fig.savefig(dest/f'{name}.png',dpi=170);fig.savefig(dest/f'{name}.svg');plt.close(fig)
    figure('wait-work',['mean_wait','p95_wait','p99_wait','completed_work'],'Synthetic time; macro mean, 95% paired-block bootstrap uncertainty')
    figure('large-small',['large_p95_wait','large_p99_wait','small_p95_wait','small_p99_wait'],'Size-stratified waiting (missing class omitted per trace)')
    figure('resources',['logical_allocation_rate_compute_units','protected_idle_capacity_time_compute_units','activation_count'],'Actual logical core allocation and separately accounted protected idle capacity')
    figure('ablations',['p95_wait','long_wait_ratio','completed_work'], 'Equal training and tuning budgets',policies=['hrrn','hrrn_adaptive_heuristic','hrrn_ppo','ppo_switch_only','ppo_no_hrrn'])
    fig,axs=plt.subplots(1,2,figsize=(16,6))
    ppo_actions=[r for r in actions if r['policy']=='hrrn_ppo'];xs=np.arange(len(ppo_actions));bottom=np.zeros(len(xs))
    for i,color in enumerate(('#718096','#277da8','#43aa8b','#e17735')):
        y=np.array([r[f'action_{i}']/max(1,r['decisions']) for r in ppo_actions]);axs[0].bar(xs,y,bottom=bottom,label=f'action {i}',color=color);bottom+=y
    axs[0].set_xticks(xs,[r['condition'].split('__')[0]+' / '+r['condition'].split('__')[-1] for r in ppo_actions],rotation=70,ha='right',fontsize=7);axs[0].legend(ncol=2);axs[0].set_title('Frozen PPO action distribution (full conditions in CSV)')
    for policy in ('hrrn','hrrn_easy','hrrn_adaptive_heuristic','hrrn_ppo'):
        data=[r for r in conditions if r['policy']==policy and r['condition'].startswith('high_load__independent')]
        data.sort(key=lambda r:('nominal','under','over','noisy').index(r['condition'].split('__')[-1]))
        axs[1].plot([r['condition'].split('__')[-1] for r in data],[r['p95_wait'] for r in data],marker='o',label=LABELS[policy])
    axs[1].legend(fontsize=8);axs[1].set_title('Estimate error sensitivity: high load, same physical traces');axs[1].set_ylabel('P95 waiting')
    fig.tight_layout();fig.savefig(dest/'actions-error.png',dpi=170);fig.savefig(dest/'actions-error.svg');plt.close(fig)
    # Actual training histories, not smoothed fictitious convergence curves.
    fig,axs=plt.subplots(1,2,figsize=(12,4.5))
    for path in sorted((out/'training').glob('*/episodes.json')):
        eps=read(path);label=path.parent.name
        axs[0].plot([e['timestep'] for e in eps],[e['reward'] for e in eps],alpha=.45,label=label)
        axs[1].plot([e['timestep'] for e in eps],[e['metrics']['p95_wait'] for e in eps],alpha=.45)
    axs[0].set_title('Training episode returns (different randomized traces)');axs[1].set_title('Training episode P95 waiting; not a convergence test')
    for ax in axs:ax.set_xlabel('Environment transitions');ax.grid(alpha=.2)
    axs[0].legend(fontsize=6,ncol=2);fig.tight_layout();fig.savefig(dest/'training.png',dpi=170);fig.savefig(dest/'training.svg');plt.close(fig)
    hs=read(out/'development/heuristic-selection.json');fig,ax=plt.subplots(figsize=(9,4))
    ax.bar([c['id'] for c in hs['candidates']],[c['objective'] for c in hs['candidates']]);ax.tick_params(axis='x',rotation=45)
    ax.set_title('Development-only heuristic sensitivity (lower objective better)');fig.tight_layout();fig.savefig(dest/'heuristic-sensitivity.png',dpi=170);fig.savefig(dest/'heuristic-sensitivity.svg');plt.close(fig)
    curves=read(out/'development/learning-diagnostics.json');fig,axs=plt.subplots(1,2,figsize=(10,4))
    for v in ('joint','switch_only','no_hrrn'):
        rr=sorted((r for r in curves if r['variant']==v),key=lambda r:r['steps'])
        axs[0].plot([r['steps'] for r in rr],[r['objective'] for r in rr],marker='o',label=v)
        axs[1].plot([r['steps'] for r in rr],[r['protect_ratio'] for r in rr],marker='o',label=v)
    axs[0].set_title('Same selected seed: development objective');axs[1].set_title('Development protection-action ratio')
    for ax in axs:ax.set_xlabel('Training transitions');ax.legend();ax.grid(alpha=.2)
    fig.tight_layout();fig.savefig(dest/'learning-diagnostics.png',dpi=170);fig.savefig(dest/'learning-diagnostics.svg');plt.close(fig)
    write_report(out,summaries,comparisons,conditions,actions)
    verify_freeze(out)
    dump(out/'delivery-verification.json',dict(freeze_verified=True,previous_artifacts_unchanged=preserved_files()==read(out/'preserved-before.json'),
        audits=sum(r['audit']['success'] for r in rows),events=sum(r['audit']['events_checked'] for r in rows),
        atomic_starts=sum(r['audit']['atomic_starts'] for r in rows),rl_decisions=sum(r['audit'].get('rl_decisions_checked',0) for r in rows),
        model_sha256s={v:m['sha256'] for v,m in read(out/'selection.json')['models'].items()}))
    return summaries

def write_report(out,summaries,comparisons,conditions,actions):
    sel=read(out/'selection.json');meta=[read(p) for p in sorted((out/'training').glob('*/metadata.json'))]
    pilot=read(out/'pilot-exclusion.json') if (out/'pilot-exclusion.json').exists() else dict(excluded_training_steps=0,excluded_training_process_seconds=0)
    repro=read(out/'training-reproducibility.json') if (out/'training-reproducibility.json').exists() else None
    ppoacts=[a for a in actions if a['policy']=='hrrn_ppo'];counts=[sum(a[f'action_{i}'] for a in ppoacts) for i in range(4)]
    header='|策略|均值等待|P95|P99|大作业P95|小作业P95|长等待%|完成工作量|保护次数/轨迹|\n|---|---:|---:|---:|---:|---:|---:|---:|---:|'
    lines=[]
    for r in summaries:
        lines.append('|'+ '|'.join([r['label']]+[f'{r[k]*(100 if k=="long_wait_ratio" else 1):.4f}' for k in ('mean_wait','p95_wait','p99_wait','large_p95_wait','small_p95_wait','long_wait_ratio','completed_work','activation_count')])+'|')
    paired='\n'.join(f"|{c['reference']}|{c['metric']}|{c['mean_difference']:.5f}|[{c['ci95_low']:.5f}, {c['ci95_high']:.5f}]|[{c['controlled_low']:.5f}, {c['controlled_high']:.5f}]|" for c in comparisons if c['primary'])
    scientific=[]
    for reference in ('hrrn','hrrn_protect','hrrn_adaptive_heuristic','hrrn_easy'):
        p=next(c for c in comparisons if c['reference']==reference and c['metric']=='p95_wait')
        status='P95稳定降低' if p['controlled_high']<0 else 'P95稳定升高（恶化）' if p['controlled_low']>0 else 'P95差异未通过多重比较控制'
        scientific.append(f"- 相对 {LABELS[reference]}：{status}，配对差 {p['mean_difference']:.4f}。")
    mp={r['policy']:r for r in summaries};x=mp['hrrn_ppo'];b=mp['hrrn']
    scientific.append(f"- 相对HRRN，大作业P95变化{x['large_p95_wait']-b['large_p95_wait']:+.4f}，小作业P95变化{x['small_p95_wait']-b['small_p95_wait']:+.4f}，完成工作量变化{x['completed_work']-b['completed_work']:+.4f}。这些分层为次要分析，区间详见CSV。")
    scientific.append(f"- PPO不保护占{counts[0]/max(1,sum(counts)):.2%}，保护占{sum(counts[1:])/max(1,sum(counts)):.2%}；{'不是字面上的永不保护策略' if sum(counts[1:]) else '本次冻结模型等价于永不保护，需要按失败结果解释'}。")
    condmap={(r['condition'],r['policy']):r for r in conditions}
    diffs=[(condmap[(c,'hrrn_ppo')]['p95_wait']-condmap[(c,'hrrn_easy')]['p95_wait'],c) for c in sorted({r['condition'] for r in conditions})]
    scientific.append(f"- 相对EASY，P95最有利条件为{min(diffs)[1]}（差{min(diffs)[0]:+.4f}），最不利条件为{max(diffs)[1]}（差{max(diffs)[0]:+.4f}）。该排序是事后描述，不作为新的显著性结论。")
    most=max(ppoacts,key=lambda a:sum(a[f'action_{i}'] for i in (1,2,3))/max(1,a['decisions']))
    least=min(ppoacts,key=lambda a:sum(a[f'action_{i}'] for i in (1,2,3))/max(1,a['decisions']))
    scientific.append(f"- PPO保护比例最高条件：{most['condition']}；最低条件：{least['condition']}。完整动作计数和错误回退原因见actions.json。")
    science='\n'.join(scientific)
    text=f'''# 第三阶段：HRRN 引导的自适应容量保护（本地 CPU 合成仿真）

这是实际 MaskablePPO 小预算实验，结论以以下原始结果为准。所有执行时长、工作量均为合成量，不能解释为真实 GPU 性能。前两阶段和 RF 均保持冻结。

## 协议与实现

训练 336 条、开发 56 条、全新独立测试 280 条（20 个种子块 ×14 条件）；每条24作业。测试9主策略及2消融，共3080次仿真。数据命名空间与第二阶段不同。测试前冻结模型、全部输入、仿真、奖励、状态、评价和本报告生成代码，见 `freeze.json`。选择记录见 `selection.json`；测试后未调参。

普通队列使用原 HRRN。超过 min(2, 预计服务时间) 且不能完整启动的最老合格作业成为候选。最多一个固定完整保护组合。动作0继续HRRN；1—3使用冻结RF排名前三个完整组合。RF评分和实际提交后端均未改变。组合未覆盖所有可能GPU集合，是固定动作空间的明确限制。

观察向量202维，包括队列、预计时长来源、3维请求、HRRN、8张卡以内的资源、预测组合释放时间、缺口、闲置和可运行作业阻塞成本。只向观察构建器传入已就绪队列和当前运行作业的公开预测；未来到达及真实结束时间不进入状态。训练集提交时长中位数归一化；测试不重新拟合。型号仅编码已知RTX4090/L40；未知型号不具备额外学习表征。

来源标签仅为submitted/history/external三种合成来源，与误差场景随机独立；观察不包含under/over/noisy等实验条件。开发期审查发现最初试跑来源标签泄露了误差类别，已整体归档于 `results/hrrn-stage3-pilot-source-labels`，不纳入任何本报告训练、开发或正式结果。修正不改变物理到达/资源/真实时长轨迹，只去除场景提示，并重新训练全部模型。归档时未执行正式测试。

奖励为完整有限 episode 的负时间积分，计入所有等待、阈值8以上的等待、大作业等待和单独的保护闲置成本。跨过阈值的积分精确分段，避免事件个数奖励。gamma=1、GAE lambda=1，有限全排空任务不做按step的时间折扣；不将不同事件间隔误当成相同物理时间。首次决策前的动作无关常数成本计入首次奖励。没有按观察窗口截断；所有作业排空后计算等待和slowdown，观察窗口只限制分配率及完成工作量。

解释性启发式在新的开发集8种规则中选择，考虑严重等待、预计近释放、闲置和立即可执行作业阻塞，不单凭等待激活。选中规则：`{sel['heuristic']['spec']}`。固定空间和整卡对照沿用第二阶段已选H_abs=12、kappa=8，避免以已知过早保护的弱参数制造优势。

## 实际训练与选择

三种观察/动作设置各用2个训练种子×2个闲置权重，每个512 transitions，共{sum(m['steps'] for m in meta)}步，训练CPU过程总耗时（各进程之和）{sum(m['wall_seconds'] for m in meta):.1f}秒。每组同样网络[64,64]、调参预算和开发评价。不是充分收敛实验，不能把单个优选种子的结果推广为训练稳定性。两种奖励权重和两个种子的完整开发结果保留。训练日志、检查点及最终权重位于 `training/`。正式PPO推理为确定性mask预测；每次推理100ms后返回才检查截止期，迟到/异常/非法输出回退0，不是抢占式硬实时保证。

此外，被排除的来源标签试跑消耗{pilot['excluded_training_steps']}步、训练进程耗时之和{pilot['excluded_training_process_seconds']:.1f}秒，计入本次实际探索成本，但不计入正式模型训练预算或最终收益。两轮总训练进程时间为{sum(m['wall_seconds'] for m in meta)+pilot['excluded_training_process_seconds']:.1f}秒；并行进程时间之和不等于墙钟时间。

预测日历做了共享查询优化后，额外对joint/seed17/idle0.25从头复训512步，逐Tensor验证与原训练权重完全相同，结果见 `training-reproducibility.json`；这次工程复现不参加模型选择。额外实际开销：{repro['rerun_process_seconds'] if repro else 0:.1f}秒。

模型选择目标固定为开发宏平均 P95 + 8×长等待比例 + 0.5×平均等待；同分按完成工作量和稳定ID。最佳模型：`{sel['models']['joint']['id']}`。并非只用训练奖励选模型。三个消融各自相同调参程度，但单个模型被开发集选中，消融结果仍有训练随机性的限制。

对三个选中种子追加相同的0/256/512步开发诊断，见 `development/learning-diagnostics.json` 和 `analysis/learning-diagnostics.png`。0步模型使用相同初始化种子和完全相同网络构造复原，256步使用原保存检查点。这些曲线只诊断是否学到有益决策，不参与重新选择检查点；正式结果仍使用512步最终模型。随机训练轨迹回报曲线与同一开发集检查点曲线分开呈现。

PPO按64步rollout分段训练，非终态段末仍用价值函数bootstrap；gamma=lambda=1消除逐事件折扣，但不消除价值估计误差。512步预算可能在一个未终止的训练episode中停止采样；这不被记成已完成episode，完整训练episode日志不包含该片段的最终统计。开发/正式评价均实际排空每个Job，不使用bootstrap替代真实后续等待，也不丢弃未完成作业。这是小预算训练近似的一项限制。

## 独立测试结果

以下P95/P99为**每条轨迹分位数的宏平均**，不是把全部Job合并的分位数。每个种子先平均14条件，再对20个块求均值。大/小规模分类共用第二阶段已冻结的 `size_class`。样本缺少某规模时该分层均值排除空轨迹，完整每轨迹计数保留。

{header}
{chr(10).join(lines)}

PPO动作次数 `[不保护,A,B,C]`：**{counts}**；不要只看回报判断是否有效。逐条件动作、判断理由和候选组合完整保留在 `analysis/actions.json` 和 `test/results.json`。

主要配对比较：PPO减去对照。等待/比例负值较好，工作量正值较好。普通95%区间及12个预先声明主要比较的Bonferroni调整近似族95%区间同时给出。bootstrap以20个配对种子块重采样100000次，条件不当独立样本；小样本bootstrap不是精确覆盖保证。这些区间以冻结模型为条件，只反映工作负载种子差异，不包含训练种子和模型选择的不确定性。

|对照|指标|配对均差|95%区间|多重控制区间|
|---|---|---:|---|---|
{paired}

完整P99、多GPU、大/小作业、bounded slowdown、窗口完成Job数、3维真实逻辑分配率及保护闲置时间、推理开销、每块SD/最小/最大、95%区间均见 `analysis/summary.csv`、`paired.csv`、`conditions.csv`。真实逻辑资源分配率与虚拟保护闲置单独记账，不能相加后称真实GPU利用率。

## 失败分析与适用范围

{science}

需基于控制区间判断是否有稳定提升，若跨0便不能宣称优于对应强基线。若动作0占绝大多数，应将结果解释为学会少保护，而非新的尾部等待突破。保护可能锁定较差GPU组合、使小作业被挡和降低窗口工作量；预测估计低估时公开截止期延展只能减少信息错误，不能保证空间保护一定有效。高负载尾部改善必须同时检查小作业等待、完成工作量和闲置成本。逐场景相对效果与过估/低估/随机误差见 `conditions.csv` 及 `actions-error.png`，不存在基于测试结果重选模型的步骤。

固定202维向量最多容纳8卡和候选作业+前三队列Job；全局队列计数保留。卡数变大时只能看前三RF组合会失去部分选择信息，更多等待作业也被聚合。这是表达限制；当前小预算结果不足以证明已形成可泛化的学习价值，暂不提出GNN替换。没有在线Volcano、Kubernetes、GPU计算或真实运行时测试。

另外执行开发集尺度压力测试：4/8张GPU ×24/40个Job，3场景×4种子×4方法，共192次。该结果不参与模型重新选择，见 `analysis/scaling-stress.csv`。它是输入维度适配和有限泛化检查，不能替代额外独立大规模测试。每次保护的真实后续等待和相同Job相对HRRN等待差见 `analysis/protection-effects.csv`。

## 文件与复现

代码与完整命令见 `gpu_lab/queue_rl/README.md`。`test/results.json` 保存每次仿真的所有动作上下文和资源审计结论；完整快照可用相同冻结模型及输入重放，原始事件引擎与独立账本逐事件验证，不用动作日志代替资源审计。模型SHA在各 `metadata.json` 和 `freeze.json`。冻结前后的旧文件SHA审计在 `preserved-before.json` 与 `delivery-verification.json`。正式结果不支持覆写；重现请复制输出目录或使用新的 `--out`。
'''
    (out/'README.md').write_text(text,encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=DEFAULT);a=p.parse_args();analyze(a.out);print(a.out/'README.md')

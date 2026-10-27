"""Standalone reports and scientific figures from actual completed results only."""
import csv
from collections import defaultdict
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from .common import OUT,OLD,SEEDS,read,dump,finite_mean

def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+
        ['| '+' | '.join(str(x) for x in row)+' |' for row in rows])

def csv_read(path):
    with Path(path).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))

def save(fig,name):
    fig.tight_layout();fig.savefig(OUT/f'figures/{name}.png',dpi=160,bbox_inches='tight');fig.savefig(OUT/f'figures/{name}.svg',bbox_inches='tight');plt.close(fig)

def plots(s,stability):
    (OUT/'figures').mkdir(exist_ok=True)
    budgets=sorted({x['budget'] for x in stability});xx=np.arange(len(budgets));colors=['#243b53','#d95f02','#1b9e77']
    metrics=['p95_wait','p99_wait','mean_wait','long_wait_ratio','small_p95_wait','completed_work']
    fig,axes=plt.subplots(2,3,figsize=(14,8))
    for ax,m in zip(axes.flat,metrics):
        for seed,color in zip(SEEDS,colors):ax.plot(xx,[s[f'ppo-{b}-seed{seed}'][m] for b in budgets],marker='o',label=f'PPO seed {seed}',color=color)
        for name,label,c in [('hrrn','HRRN','#555555'),('hrrn_easy','EASY','#b2182b'),('hrrn_adaptive_heuristic','Heuristic','#762a83'),('hrrn_protect','Fixed protection','#a6761d')]:
            ax.axhline(s[name][m],ls='--',color=c,label=label)
        ax.set(xticks=xx,xticklabels=budgets,xlabel='Budget checkpoint (equal spacing)',title=m);ax.grid(alpha=.2)
    axes.flat[0].legend(fontsize=8);fig.suptitle('Independent development: same 168 traces, three training seeds; synthetic time',y=1.01)
    save(fig,'learning-curves')
    fig,axes=plt.subplots(1,3,figsize=(14,4))
    for ax,m in zip(axes,('conditional_protect','policy_entropy','protect_probability')):
        for seed,color in zip(SEEDS,colors):ax.plot(xx,[s[f'ppo-{b}-seed{seed}'].get(m,0) for b in budgets],marker='o',label=str(seed),color=color)
        ax.set(xticks=xx,xticklabels=budgets,xlabel='Budget checkpoint (equal spacing)',title=m);ax.grid(alpha=.2)
    axes[0].legend(title='Training seed');save(fig,'behavior-curves')
    fig,axes=plt.subplots(2,2,figsize=(12,8))
    for seed,color in zip(SEEDS,colors):
        folder=OUT/f'training/seed-{seed}-full32768' if (OUT/f'training/seed-{seed}-full32768/metadata-32768.json').exists() else OUT/f'training/seed-{seed}'
        u=read(folder/'updates.json')
        for ax,m in zip(axes.flat,('train/value_loss','advantage_std','entropy','parameter_delta_norm')):
            ax.plot([x['step'] for x in u],[x[m] for x in u],label=str(seed),color=color,alpha=.85);ax.set(title=m,xlabel='Transitions');ax.grid(alpha=.2)
    axes.flat[0].legend(title='Training seed');save(fig,'update-diagnostics')
    labels=['hrrn','hrrn_easy','hrrn_protect','hrrn_adaptive_heuristic']+[f'ppo-{budgets[-1]}-seed{x}' for x in SEEDS]
    fig,axes=plt.subplots(1,2,figsize=(12,4.8))
    for ax,m,title in zip(axes,('logical_allocation_rate_compute_units','protected_idle_capacity_time_compute_units'),('Actual logical core quota allocation (fraction)','Protected but idle core quota x synthetic time')):
        ax.bar(range(len(labels)),[s[x][m] for x in labels],color='#377eb8');ax.set_xticks(range(len(labels)),labels,rotation=35,ha='right',fontsize=8);ax.set_title(title,fontsize=10)
    save(fig,'allocation-and-idle')
    episodes=csv_read(OUT/'diagnosis/protection-episodes.csv')
    chosen=max((r for r in episodes if int(r['gpu_count'])>1),key=lambda r:float(r['observed_delay']))
    samples=[r for r in csv_read(OUT/'diagnosis/resource-accumulation.csv') if all(r[k]==chosen[k] for k in ('seed','condition','epoch'))]
    fig,axes=plt.subplots(1,3,figsize=(12,4))
    for ax,dim in zip(axes,('memory_mib','compute_units','slots')):
        for gpu in sorted({r['gpu'] for r in samples}):
            rr=[r for r in samples if r['gpu']==gpu and r['dimension']==dim]
            ax.step([float(r['time'])-float(chosen['activation']) for r in rr],[float(r['protected_idle']) for r in rr],where='post',label=gpu)
            ax.axhline(float(rr[0]['required']),ls=':',color='black',alpha=.35)
        ax.set(title=dim,xlabel='Synthetic time since protection');ax.grid(alpha=.2)
    axes[0].legend(fontsize=7);fig.suptitle('One audited multi-GPU guard: accumulated idle capacity; dotted = required')
    save(fig,'resource-accumulation')

def diagnostic_report():
    d=read(OUT/'diagnosis/summary.json');acc=read(OUT/'diagnosis/accumulation-summary.json')
    cf=read(OUT/'diagnosis/counterfactual-summary.json') if (OUT/'diagnosis/counterfactual-summary.json').exists() else None
    cf_text=(f"额外完成 {cf['cases']} 个旧保护状态、{cf['branches']} 个合法动作分支的单步干预回放。"
        f"其中 {cf['cases_any_protection_improves_p95']} 个状态存在比不保护更低P95的保护动作，"
        f"{cf['cases_any_protection_improves_p95_with_bounds']} 个同时满足预设的小作业/长等待/工作量边界；"
        f"原模型实际动作在 {cf['original_protection_improves_p95']} 个状态改善P95。"
        "这是使用完整合成轨迹、事后比较真实合成结果的离线诊断，未来事件没有进入PPO观察、训练或模型选择。"
        "样本在查看反事实结果前固定为每个有保护场景的最小两个原种子、各自第一次保护；它偏向原模型实际保护状态，不代表全体决策。"
        "分支保留相同前缀，只改变一次动作，此后继续冻结原PPO。完整记录见 offline-counterfactual.csv；不能把事后最佳分支称为可部署策略。") if cf else '本轮未执行单步反事实回放。'
    candidates=csv_read(OUT/'diagnosis/candidate-comparison.csv');training=csv_read(OUT/'diagnosis/original-training-and-seeds.csv')
    t=[]
    for r in training:
        # A dummy transition terminates immediately and is necessarily logged.
        # Unaccounted final transitions therefore belong to a real partial episode.
        r['exact_effective_inferred']=int(r['steps'])-int(r['completed_no_decision_episodes'])
        t.append([r['model'],r['completed_episode_effective'],r['exact_effective_inferred'],r['dev_actions'],f"{float(r['dev_conditional_protection']):.2%}"])
    text=f'''# 原 PPO 学习有效性独立诊断

只分析已查看的原第三阶段结果。此报告不是新的盲测，不参与新训练预算选择。
所有时长和工作量均为合成数据。原始目录、模型、失败结果和统计文件保留。

## 动作有效性

原 selected joint 模型共有 {d['decisions']} 次决策，每次至少一个保护动作合法。

{table(['动作','合法次数','合法比例','实际选择次数'],[[label,d['legal_counts'][i],f"{d['legal_ratio'][i]:.2%}",d['chosen'][i]] for i,label in enumerate(['0','A','B','C'])])}

条件保护率为 **{d['conditional_protection']:.4%}**。C 在 {d['c_legal_but_not_selected']} 次合法机会中仍未被确定性策略选中。
C 的合法机会均为单卡作业：当前四卡分属两个双卡节点，多卡完整组合通常只有两组，因此 C 在多卡情况下结构性缺席。
这说明 C 的零选择既包含候选数量限制，也包含合法时不被 argmax 选中的模型偏好，不能全部归为 mask。

## GPU 组合的同状态比较

以下仅取 A/B/C 同时合法的 2916 个相同决策状态。缺口和空闲均按集群容量归一化，空闲列为三维平均；RF 分数未改动。

{table(['候选','预计凑齐延迟','显存缺口','算力缺口','槽位缺口','保护空闲份额','当前被阻塞作业数','RF分数','模型概率'],[[('A','B','C')[i]]+[f"{float(r['paired_ABC_'+k]):.4f}" for k in ('delay','deficit_memory','deficit_core','deficit_slots','idle_fraction','blocked','rf_score','probability')] for i,r in enumerate(candidates)])}

这些是公开预测量，不是未来真实释放时间；三维空闲份额乘以预测延迟只能作为粗略成本指标，不能视为实际积分成本。
完整 CSV 还提供各候选全部合法状态的结果，避免混淆不同 mask 群体。C 并非在所有公开指标上都劣于 A/B，但在确定性选择中从未胜出。
动作只能选择“不保护/保护当前最长等待对象的前三个RF完整组合”，不能换保护对象、修改排队顺序、撤销已激活保护或实施时间回填；状态只保留四个队列位置和聚合信息。
因此本实验不能区分“任意空间保护都无效”与“这个有限动作/状态设计不够灵活”。

## 每次资源积累与实际启动差异

已经逐一复放 **{acc['episodes']} 次保护、{acc['traces']} 条轨迹**，与保存的原动作、时间及目标 Job 一致，独立账本审计通过。
每个目标 GPU 每一维受保护空闲容量在保护对象启动前单调不减，未被后续作业侵占。
逐事件、逐 GPU、逐维 required/free/missing/protected_idle 见 `resource-accumulation.csv`；每次保护摘要见 `protection-episodes.csv`。
平均公开预计凑齐延迟 {acc['mean_predicted_delay']:.4f}，实际观察延迟 {acc['mean_observed_delay']:.4f}。实际延迟仅用于离线评估，不进入策略。

同一完整轨迹上的 6720 个 Job：{d['jobs']['earlier']} 个提前、{d['jobs']['later']} 个延后、{d['jobs']['same']} 个不变。
提前量合计 {d['sum_start_benefit']:.4f}；延后量合计 {d['sum_start_loss']:.4f}。
被保护 Job 中：提前 {d['protected_jobs']['earlier']}、延后 {d['protected_jobs']['later']}、不变 {d['protected_jobs']['same']}。
这些是两种策略的全轨迹差异，受之前的所有调度决策影响，不是隔离单次保护的因果效应。
{cf_text}

## 512 步究竟有多少有效决策

{table(['模型','已完成Episode的有效决策','含末尾未结束Episode的有效决策','旧开发集0/A/B/C','条件保护率'],t)}

最后一列有效决策由精确记账推得：无决策 Episode 在一个 dummy transition 后立即终止，必定进入保存的 Episode 日志；因此 `512 - 无决策Episode数` 包括末尾未结束 Episode 的全部有效决策。
例如原 joint-idle1-seed17 为 **504/512**，其中503已出现在完整Episode记录、1在末尾未完成Episode。有效机会并不稀少，但有效机会不等于动作确实有显著不同的长期收益。

## 优化器与概率诊断的证据边界

冻结最终策略在原公开状态上的平均熵为 {d['entropy']:.4f} nats，仍然较高。
该概率是用冻结最终模型重新计算，**不是原训练时保存的概率**。确定性评估使用 argmax，微小概率差也能造成近乎全选0；不能据此认定随机训练策略已经全选0。
原 progress.csv 显示非零KL、有限价值损失和策略损失，没有直接的优化器停止更新证据；低 explained variance 提示价值预测仍有限。
旧 CSV 最后一行在最终 train() 前输出，`n_updates=42` 不代表总共只有42次；512步有8个64步 rollout，每个6轮，共48个优化epoch。
旧模型没有保存原始优势估计或逐次参数变化，无法事后精确补回；本轮新增训练记录这些量，并通过参数逐位相等测试确认遥测和分段 learn() 不改变原 PPO 更新。

不同原训练种子和 idle 权重并没有形成稳定一致的保护偏好：多个 joint 模型在旧开发集完全不保护；另一些偏好 A 或 B。
故“512步足以收敛”“C永远被mask”“主要是有效动作不足”均不受现有证据支持。

## 文件

- `per-action.csv`：逐机会逐动作合法性、选择、概率、公开候选指标。
- `same-job-starts.csv`：PPO/HRRN同Job启动时间差。
- `original-training-and-seeds.csv`：原12模型日志与行为对照。
- `resource-accumulation.csv`、`protection-episodes.csv`：全部实际保护事件的离线复放。
'''
    (OUT/'diagnosis/README.md').write_text(text,encoding='utf-8')

def main_report():
    s=read(OUT/'analysis/summary.json');st=read(OUT/'analysis/stability.json');train=read(OUT/'analysis/training.json')
    paired=read(OUT/'analysis/paired.json');gate=read(OUT/'staircase-decision.json');verification=read(OUT/'analysis/verification.json')
    budgets=sorted({x['budget'] for x in st});last=budgets[-1]
    if any(v['passes'] for v in verification['promotions'].values()):
        raise RuntimeError('Promotion passed: must freeze and run a new test before producing the final decision report')
    plots(s,st);diagnostic_report()
    metrics=['mean_wait','p95_wait','p99_wait','long_wait_ratio','small_p95_wait','large_p95_wait','completed_work']
    rows=[]
    for name in ('hrrn','hrrn_easy','hrrn_protect','hrrn_adaptive_heuristic'):
        rows.append([name]+[f"{s[name][m]:.4f}" for m in metrics])
    for b in budgets:
        rows.append([f'PPO {b}（3种子均值）']+[f"{np.mean([s[f'ppo-{b}-seed{x}'][m] for x in SEEDS]):.4f}" for m in metrics])
    pp=[r for r in paired if r['model']==f'ppo-{last}-seedmean' and r['metric'] in ('p95_wait','mean_wait','small_p95_wait','long_wait_ratio','completed_work')]
    metadata_rows=[[m['seed'],m['steps'],m['episodes'],m['effective_decisions'],m['dummy_transitions'],f"{m['wall_seconds']:.1f}",f"{m['cpu_seconds']:.1f}",m['sha256']] for m in train]
    stability_rows=[]
    for b in budgets:
        for seed in SEEDS:
            x=s[f'ppo-{b}-seed{seed}'];stability_rows.append([b,seed,f"{x['p95_wait']:.4f}",f"{x['mean_wait']:.4f}",f"{x['conditional_protect']:.2%}",str(x['action_counts']),f"{x.get('policy_entropy',0):.4f}"])
    complete=[read(p) for p in (OUT/'training').glob('*/complete.json')]
    last_updates=[]
    for seed in SEEDS:
        suffix='-full32768' if last==32768 else ''
        u=read(OUT/f'training/seed-{seed}{suffix}/updates.json')
        last_updates.append(dict(seed=seed,updates=len(u),all_finite=all(x['all_finite'] for x in u),
            all_parameters_changed=all(x['parameter_delta_norm']>0 for x in u),
            **{k:u[-1][k] for k in ('train/value_loss','train/explained_variance','entropy','advantage_std','parameter_delta_norm')}))
    dump(OUT/'analysis/update-health.json',last_updates)
    last_gain=1-np.mean([s[f'ppo-{last}-seed{x}']['p95_wait'] for x in SEEDS])/np.mean([s[f'ppo-8192-seed{x}']['p95_wait'] for x in SEEDS])
    cf=read(OUT/'diagnosis/counterfactual-summary.json') if (OUT/'diagnosis/counterfactual-summary.json').exists() else None
    conclusion='停止当前强化学习路线，转而完善已验证的HRRN队列机制。'
    text=f'''# HRRN-PPO 学习有效性与训练预算实验

**本轮判断：{conclusion}**

这是一项基于新训练/开发数据的阶段性工程与研究取舍，不是“PPO永远学不会”或“空间保护不可能有效”的证明。
最高实际完成预算为每种子 **{last} transitions**，3个训练种子；较512步增加 {last/512:g} 倍。没有通过预先声明的晋级条件。
{'按预先声明的停滞规则，未执行32768步，也未生成或运行新的最终测试。' if gate['stop_before32768'] else '8192步相对2048步的均值仍有改善，因此按冻结规则执行了32768步；结果仍未达到晋级门槛，没有生成或运行新的最终测试。'}
旧第三阶段正式测试仅用于行为诊断，未用于本轮预算或模型选择；它没有被重新命名为新盲测。

## 实现与协议

新增独立 `gpu_lab/queue_rl_budget/`，复用冻结 HRRN、EASY、完整Job提交、三维资源账本和RF候选后端。
joint 202维观察、0/A/B/C动作、[64,64]网络、学习率3e-4、64步rollout、32 batch、6 epochs、gamma=lambda=1、熵系数0.01、idle奖励权重1均保持原定义。
连续分段训练在512、2048、8192处保存**完成该预算更新之后**的模型；128步逐位一致性测试验证分段与遥测不改变更新。
奖励和归一化沿用原模型；并未为了更好的开发结果重新调奖励、阈值或RF。
336条新训练轨迹，168条新开发轨迹（12个工作负载种子块×14场景/误差组合），与旧train/dev/test种子均不重叠；生成分布相同。
网络训练种子为17、43、71。未训练PPO是各训练种子自身的初始模型，不是另选随机初值。
冻结的输入SHA和评价门槛见 `input-freeze.json`、`protocol.json`，时间早于本轮训练/开发评价。

主要指标为全Job等待P95；晋级需至少2/3训练种子同时：比HRRN改善≥3%，比最好的EASY/固定保护/启发式改善≥1%；小Job P95增长不超过max(0.05,5%)，长等待比例增加≤0.5个百分点，窗口完成工作量损失≤1%。此外三种子均值需比512步改善≥1%、比未训练改善≥3%。
这些是预先声明的研究投入边界，不是看完结果后选择的胜负标准。

## 新开发集完整比较

等待单位为合成时间，长等待列为比例，工作量为合成 core-quota×GPU数×时长，不是FLOPs。所有等待指标在轨迹完整排空后统计；完成工作量按原固定窗口。

{table(['方法','平均等待','P95','P99','长等待比例','小Job P95','大Job P95','窗口完成工作量'],rows)}

![学习曲线](figures/learning-curves.png)

PPO不能仅凭比未训练随机偏好更好就宣称有调度收益，必须同时看相同轨迹上的HRRN、EASY和简单自适应规则。
各条件（含低负载、高负载、估计低估/高估/随机误差、多GPU）完整对照在 `analysis/conditions.csv`，不得把其中个别有利场景改写为主要胜利标准。

## 种子稳定性与动作变化

{table(['预算','训练种子','开发P95','开发平均等待','条件保护率','0/A/B/C次数','有决策轨迹宏平均熵'],stability_rows)}

![保护行为](figures/behavior-curves.png)

训练阶段按概率采样，开发阶段沿用原确定性argmax；两者的保护比例不可混为一谈。
条件保护率按全部合法机会汇总；熵/保护概率先在每条有决策的轨迹内平均，再对这些轨迹平均，不把无决策轨迹当零熵。
`analysis/stability.csv` 给出三种子均值、标准差、最小/最大；不能把同一轨迹的三个训练模型当成三个独立工作负载样本。

## 配对差异及不确定性

以下为最高预算PPO先对3个训练种子求均值，再对12个相同工作负载种子块配对bootstrap（10000次）的探索性95%区间。没有作正式盲测显著性声明；区间反映工作负载抽样，不包括所有潜在训练随机性。
差值=PPO−对照，等待/长等待比例越负越好，完成工作量越正越好。

{table(['对照','指标','配对差值','95%区间'],[[r['comparator'],r['metric'],f"{r['delta']:.5f}",f"[{r['low']:.5f}, {r['high']:.5f}]"] for r in pp])}

## 训练预算和成本

{table(['种子','steps','完成Episode','有效保护决策','dummy','累计wall秒','累计CPU秒','模型SHA256'],metadata_rows)}

以上checkpoint成本为各训练进程累计值，不能把同一模型的512/2048/8192成本相加。实际完整训练进程CPU合计 {sum(x['cpu_seconds'] for x in complete):.1f} 秒；进程wall合计 {sum(x['wall_seconds'] for x in complete):.1f} 秒，三进程并行，wall和CPU不同。
训练成本包含必要遥测与中间保存，不包含另行评估；开发执行wall含等待checkpoint，见 `development-execution.json`。
若执行32768，完整重训的重复8192前缀也已计入上述成本；真正最终预算仍是每个模型32768，而不是将两次进程steps相加称为40960。重复前缀参数、优化器状态和最后观察的逐位校验见 `extension-prefix-verification.json`。
逐步概率、mask、raw advantage、value、return、reward见各种子 `steps.csv`，公开观察保存在 `observations.npz`；逐次更新损失、KL、entropy、参数差范数和finite检查见 `updates.json`。

![训练更新](figures/update-diagnostics.png)

{table(['种子','更新批次数','全部finite','每次参数均变化','最终value loss','最终explained variance','最终rollout熵','最终优势标准差'],[[x['seed'],x['updates'],x['all_finite'],x['all_parameters_changed'],f"{x['train/value_loss']:.5f}",f"{x['train/explained_variance']:.5f}" if x['train/explained_variance'] is not None else 'NA',f"{x['entropy']:.5f}",f"{x['advantage_std']:.5f}"] for x in last_updates])}

8192相对2048的三种子平均P95改善为 **{gate['mean_p95_improvement_2048_to8192']:.3%}**；各种子改善为 {', '.join(f'{x:.3%}' for x in gate['seed_p95_improvements'])}。
晋级结果和每一条损失边界是否满足完整写在 `staircase-decision.json` 与 `analysis/verification.json`。
最高预算相对8192的平均P95改善为 {last_gain:.3%}。若出现近零熵或极小梯度，应结合此表与完整更新曲线理解为策略分布趋于集中，不自动视为程序没有执行更新。
总参数差也包含critic，不能据此宣称actor每次都有有效学习；`analysis/checkpoint-actor-critic.csv` 分别核对各预算间actor/critic变化。接近零的策略梯度和熵意味着探索已收缩，负explained variance表示该rollout上价值预测不足；两者均不能包装成稳定超越启发式的证据。
更长训练的证据应读作“预算增大后的实际学习与波动”，不能用学习曲线的最低单点替代稳定改善。

## 原模型诊断与原因判断

独立报告见 [diagnosis/README.md](diagnosis/README.md)。原joint模型有14156次有效保护机会，C合法2916次但零选择。
512步中原selected joint实际上有504次有效选择（8次dummy），因此有效动作稀少不是主要瓶颈；总数据量小和状态覆盖有限仍然可能影响学习。
原模型熵仍高，只有512步确实不足以证明收敛；本轮扩大预算用于检查该解释，而不是预设训练不足就是唯一原因。
全部242次原实际保护均已离线复放：资源逐维积累、固定完整组合、后续作业不侵占和完整Job启动机制正确，但这不保证被保护Job相对无保护总是更早，更不保证全队列尾部更好。

当前奖励优化全队列等待/长等待/大作业等待及空闲成本的积分，并不直接优化P95分位数；固定保护对象、前三候选和保护期间不可重新决策限制了动作表达。
空间预留会闲置容量并改变后续作业排队次序，等待收益与损失并存。增加表示网络复杂度不能自动消除这种机制性代价或目标不一致。
{'补充离线单步干预：25个旧实际保护状态中，15个存在可降低P95的合法保护分支，但只有4个同时满足代价边界，原模型实际动作仅3个降低P95。这些事后未来感知结果只说明局部机会确实存在，不能作为可部署oracle、训练目标或新测试成绩。' if cf else ''}
由于本轮保持奖励和动作定义不变，不能单凭结果确定是动作限制还是空间保护的一般性限制；能确定的是**当前定义下没有足够、稳定、满足代价边界的学习收益支撑继续增加复杂度**。

## 资源与适用范围

![实际逻辑分配与保护空闲](figures/allocation-and-idle.png)

实际逻辑分配率和受保护空闲容量分别统计，二者之和不称为真实GPU利用率。
保护机制适合作为可验证的长等待保障手段，在明确的大作业服务目标、等待阈值和可接受小作业损失约束下讨论；不应宣称其自然改善整体公平性或吞吐。
当前建议完善HRRN的完整Job可行性旁路、等待指标、保护成本与失效清理、与同粒度EASY的取舍；保留负结果。**不建议现在引入GNN或DQN，也不进入真实调度器部署。**

## 测试、保存与复现

本轮新开发评价 {verification['runs']} 次，独立资源/完整提交/保护/公开预测审计全部通过。单元测试执行结果另见 `unit-tests.xml`。
原第三阶段及前两阶段完整性校验见 `preservation-verification.json`，新输入校验见 `input-freeze.json`。
未执行新最终测试：没有满足晋级门槛，故不存在新盲测结论或新最终模型选择。

运行方式见新增代码目录的 README；已完成输出禁止覆盖。所有实验仅本地CPU离散事件仿真，RandomForest、真实GPU和生产集群均未修改。
'''
    (OUT/'README.md').write_text(text,encoding='utf-8')

if __name__=='__main__':main_report()

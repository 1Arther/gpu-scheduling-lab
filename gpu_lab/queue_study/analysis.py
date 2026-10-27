"""Frozen evaluation and scientific plots; never selects test parameters."""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import csv,json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from .experiments import check_freeze,write_json,write_csv
from .simulator import POLICIES

NAMES={'fifo':'FIFO','sjf':'SJF','hrrn':'HRRN','hrrn_age':'HRRN+age','hrrn_easy':'HRRN+EASY',
       'hrrn_whole_gpu':'HRRN+whole','hrrn_protect':'HRRN+vGPU'}
ORDER=('fifo','sjf','hrrn','hrrn_age','hrrn_easy','hrrn_whole_gpu','hrrn_protect')
COLORS=('#8090a2','#859e91','#547daa','#a294ba','#ddab55','#bc7474','#437f71')
METRICS=('mean_wait','p95_wait','p99_wait','long_wait_ratio','mean_bounded_slowdown','completed_count','completed_work',
         'large_p95_wait','large_p99_wait','small_p95_wait','small_p99_wait','multi_gpu_p95_wait','multi_gpu_p99_wait',
         'small_large_mean_wait_gap','logical_allocation_rate_memory_mib','logical_allocation_rate_compute_units',
         'logical_allocation_rate_instance_slots','logical_protection_reservation_rate_memory_mib',
         'logical_protection_reservation_rate_compute_units','logical_protection_reservation_rate_instance_slots',
         'protected_idle_capacity_time_memory_mib','protected_idle_capacity_time_compute_units','protected_idle_capacity_time_instance_slots',
         'decision_ms_mean','decision_ms_p95','activation_mean_predicted_delay','activation_mean_target_penalty','easy_reservation_delays',
         'activation_near_count','activation_far_count','protected_idle_core_time_near','protected_idle_core_time_far',
         'target_penalty_positive_count','other_jobs_bypassed_during_protection')


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        rows=[]
        for row in csv.DictReader(f):
            for k,v in row.items():
                if v=='':row[k]=None;continue
                try:row[k]=float(v)
                except ValueError:pass
            rows.append(row)
        return rows


def blocks(rows,metric):
    """Equal scenario weight, equal condition weight inside each scenario."""
    groups=defaultdict(lambda:defaultdict(list))
    for r in rows:
        if r.get(metric) is not None:groups[r['seed']][r['scenario']].append(r[metric])
    return {s:float(np.mean([np.mean(v) for v in scenes.values()])) for s,scenes in groups.items()}


def interval(values,level=.95,draws=4000,tag=''):
    a=np.array(values,dtype=float)
    if not len(a):return None,None
    seed=int.from_bytes(sha256(tag.encode()).digest()[:8],'big')
    rng=np.random.default_rng(seed)
    means=a[rng.integers(0,len(a),(draws,len(a)))].mean(axis=1)
    return tuple(float(v) for v in np.quantile(means,((1-level)/2,1-(1-level)/2)))


def describe(rows,metric,tag=''):
    b=blocks(rows,metric);a=list(b.values())
    if not a:return dict(mean=None,sd=None,minimum=None,maximum=None,ci_low=None,ci_high=None,n=0)
    lo,hi=interval(a,tag=tag+metric)
    return dict(mean=float(np.mean(a)),sd=float(np.std(a,ddof=1)) if len(a)>1 else 0.,minimum=min(a),maximum=max(a),
                ci_low=lo,ci_high=hi,n=len(a))


def paired(rows,comparator,metric):
    a=blocks([r for r in rows if r['policy']=='hrrn_protect'],metric)
    b=blocks([r for r in rows if r['policy']==comparator],metric)
    seeds=sorted(set(a)&set(b));d=[a[s]-b[s] for s in seeds]
    lo,hi=interval(d,tag=comparator+metric)
    primary=metric in ('p95_wait','long_wait_ratio','completed_work')
    flo,fhi=interval(d,1-.05/9,40000,tag='family'+comparator+metric) if primary else (None,None)
    baseline=float(np.mean([b[s] for s in seeds])) if seeds else None
    return dict(comparator=comparator,metric=metric,n=len(seeds),difference=float(np.mean(d)) if d else None,
        ci_low=lo,ci_high=hi,primary=primary,family_ci_low=flo,family_ci_high=fhi,
        relative_difference=float(np.mean(d))/baseline if baseline and d else None,
        direction='higher is better' if metric in ('completed_work','completed_count') else 'lower is better')


def savefig(fig,path):
    fig.savefig(str(path)+'.png',dpi=180,bbox_inches='tight')
    fig.savefig(str(path)+'.svg',bbox_inches='tight');plt.close(fig)


def bars(ax,rows,metric,title,scale=1.):
    ds=[describe([r for r in rows if r['policy']==p],metric,p) for p in ORDER]
    vals=[(d['mean'] or 0)*scale for d in ds]
    err=np.array([[max(0,(d['mean'] or 0)-(d['ci_low'] or 0))*scale for d in ds],
                  [max(0,(d['ci_high'] or 0)-(d['mean'] or 0))*scale for d in ds]])
    ax.bar(np.arange(len(ORDER)),vals,color=COLORS,yerr=err,capsize=2)
    ax.set_xticks(np.arange(len(ORDER)),[NAMES[p] for p in ORDER],rotation=30,ha='right',fontsize=8)
    ax.set_title(title,fontsize=11);ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)


def figures(out,rows,development):
    out=Path(out);out.mkdir(exist_ok=False)
    fig,axes=plt.subplots(2,2,figsize=(13,8))
    for ax,metric,title in zip(axes.flat,('mean_wait','p95_wait','p99_wait','completed_work'),
        ('Mean queue wait','Mean per-trace P95 wait','Mean per-trace P99 wait','Completed synthetic core-quota work')):
        bars(ax,rows,metric,title)
    fig.suptitle('20 paired seed blocks; scenario-balanced means and 95% bootstrap CI',fontsize=12)
    fig.tight_layout();savefig(fig,out/'01-wait-and-work')
    fig,axes=plt.subplots(2,3,figsize=(15,8))
    for i,group in enumerate(('small','large','multi_gpu')):
        for y,q in enumerate(('p95','p99')):bars(axes[y,i],rows,f'{group}_{q}_wait',f'{group}: mean {q.upper()} wait')
    fig.tight_layout();savefig(fig,out/'02-size-tail-wait')
    fig,axes=plt.subplots(2,3,figsize=(15,8))
    for i,dim in enumerate(('memory_mib','compute_units','instance_slots')):
        bars(axes[0,i],rows,'logical_allocation_rate_'+dim,dim+': actual logical allocation (%)',100)
        bars(axes[1,i],rows,'logical_protection_reservation_rate_'+dim,dim+': idle spatial protection (%)',100)
    fig.suptitle('Allocated commitments and virtual protection shown separately; no measured GPU utilization',fontsize=11)
    fig.tight_layout();savefig(fig,out/'03-allocated-and-idle-protection')
    sensitivity=read_csv(Path(development)/'parameter-sensitivity.csv');hs=sorted({r['H_abs'] for r in sensitivity});ks=sorted({r['kappa'] for r in sensitivity})
    fig,axes=plt.subplots(1,3,figsize=(13,4))
    for ax,metric,title in zip(axes,('p95_wait','mean_wait','completed_work'),('Development P95 wait','Development mean wait','Development completed work')):
        a=np.array([[next(r[metric] for r in sensitivity if r['H_abs']==h and r['kappa']==k) for k in ks] for h in hs])
        im=ax.imshow(a,aspect='auto',cmap='viridis');ax.set_xticks(range(len(ks)),ks);ax.set_yticks(range(len(hs)),hs)
        ax.set_xlabel('kappa');ax.set_ylabel('H_abs');ax.set_title(title,fontsize=10);fig.colorbar(im,ax=ax,shrink=.7)
        for (i,j),v in np.ndenumerate(a):ax.text(j,i,f'{v:.2f}',ha='center',va='center',fontsize=8,color='white' if v<a.mean() else 'black')
    fig.tight_layout();savefig(fig,out/'04-development-threshold-sensitivity')
    fig,axes=plt.subplots(2,2,figsize=(12,8));errors=('nominal','under','over','noisy')
    for ax,metric,title in zip(axes.flat,('p95_wait','small_p95_wait','mean_bounded_slowdown','completed_work'),
        ('P95 wait','Small-job P95 wait','Mean bounded slowdown','Completed synthetic work')):
        for p in ('hrrn','hrrn_easy','hrrn_whole_gpu','hrrn_protect'):
            ds=[describe([r for r in rows if r['policy']==p and r['scenario']=='high_load' and r['error']==e],metric,p+e) for e in errors]
            ax.errorbar(range(4),[d['mean'] for d in ds],yerr=np.array([[d['mean']-d['ci_low'] for d in ds],[d['ci_high']-d['mean'] for d in ds]]),label=NAMES[p],marker='o',capsize=2)
        ax.set_xticks(range(4),errors);ax.set_title('High load: '+title);ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.tight_layout();savefig(fig,out/'05-estimation-error')
    scenarios=sorted({r['scenario'] for r in rows});comparators=('hrrn','hrrn_easy','hrrn_whole_gpu')
    fig,axes=plt.subplots(1,3,figsize=(13,6))
    for ax,metric,title in zip(axes,('p95_wait','large_p95_wait','completed_work'),('P95 difference','Large P95 difference','Completed work difference')):
        data=np.array([[paired([r for r in rows if r['scenario']==s],p,metric)['difference'] for p in comparators] for s in scenarios])
        vmax=max(.01,float(np.nanmax(np.abs(data))));im=ax.imshow(data,cmap='RdBu_r' if metric!='completed_work' else 'RdBu',vmin=-vmax,vmax=vmax,aspect='auto')
        ax.set_xticks(range(3),[NAMES[p] for p in comparators],rotation=30,ha='right');ax.set_yticks(range(len(scenarios)),scenarios)
        ax.set_title('vGPU minus baseline: '+title,fontsize=9);fig.colorbar(im,ax=ax,shrink=.7)
        for (i,j),v in np.ndenumerate(data):ax.text(j,i,f'{v:+.2f}',ha='center',va='center',fontsize=8)
    fig.tight_layout();savefig(fig,out/'06-scenario-failures')
    fig,axes=plt.subplots(1,3,figsize=(14,4))
    for ax,corr in zip(axes,('independent','positive','negative')):
        bars(ax,[r for r in rows if r['correlation']==corr and r['error']=='noisy'],'p95_wait',corr+': P95 wait')
    fig.tight_layout();savefig(fig,out/'07-resource-duration-correlation')
    profile=Path(development)/'serial-profile.csv'
    if profile.exists():
        rr=read_csv(profile);fig,ax=plt.subplots(figsize=(9,4))
        vals=[np.mean([r['decision_ms_mean'] for r in rr if r['policy']==p]) for p in ORDER]
        sd=[np.std([r['decision_ms_mean'] for r in rr if r['policy']==p],ddof=1) for p in ORDER]
        ax.bar(range(7),vals,color=COLORS,yerr=sd,capsize=3);ax.set_xticks(range(7),[NAMES[p] for p in ORDER],rotation=20)
        ax.set_ylabel('ms per instrumented decision');ax.set_title('Serial CPU profiling: 4 development conditions x 3 repeats; bars = SD')
        fig.tight_layout();savefig(fig,out/'08-serial-decision-overhead')
    fig,axes=plt.subplots(1,2,figsize=(10,4))
    for i,p in enumerate(('hrrn_whole_gpu','hrrn_protect')):
        vals=[describe([r for r in rows if r['policy']==p],'protected_idle_core_time_'+g,p+g)['mean'] for g in ('near','far')]
        axes[0].bar(np.array([0,1])+(i-.5)*.3,vals,width=.3,label=NAMES[p])
        counts=[describe([r for r in rows if r['policy']==p],'activation_'+g+'_count',p+g)['mean'] for g in ('near','far')]
        axes[1].bar(np.array([0,1])+(i-.5)*.3,counts,width=.3,label=NAMES[p])
    for ax in axes:ax.set_xticks((0,1),('Predicted wait <= 2','Predicted wait > 2'));ax.legend(fontsize=8)
    axes[0].set_title('Idle protected core-quota x synthetic time');axes[1].set_title('Mean number of protection activations')
    fig.suptitle('Near/far classification uses estimated, not hidden actual releases',fontsize=11)
    fig.tight_layout();savefig(fig,out/'09-near-far-protection-cost')


def analyze(out,development):
    out=Path(out);check_freeze(out);rows=read_csv(out/'runs.csv')
    summary=[]
    for policy in ORDER:
        rr=[r for r in rows if r['policy']==policy]
        for metric in METRICS:summary.append(dict(policy=policy,metric=metric,**describe(rr,metric,policy)))
    write_csv(out/'summary.csv',summary)
    pairs=[paired(rows,p,m) for p in ('hrrn','hrrn_easy','hrrn_whole_gpu') for m in METRICS]
    write_csv(out/'paired-comparisons.csv',pairs)
    scenario=[]
    for s in sorted({r['scenario'] for r in rows}):
        for p in ORDER:
            for m in METRICS:scenario.append(dict(scenario=s,policy=p,metric=m,**describe([r for r in rows if r['scenario']==s and r['policy']==p],m,s+p)))
    write_csv(out/'by-scenario.csv',scenario)
    condition=[]
    for c in sorted({r['condition'] for r in rows}):
        for p in ORDER:
            for m in ('mean_wait','p95_wait','p99_wait','long_wait_ratio','large_p95_wait','small_p95_wait','multi_gpu_p95_wait','completed_work'):
                condition.append(dict(condition=c,policy=p,metric=m,**describe([r for r in rows if r['condition']==c and r['policy']==p],m,c+p)))
    write_csv(out/'by-condition.csv',condition)
    failures=[]
    for s in sorted({r['scenario'] for r in rows}):
        for p in ('hrrn','hrrn_easy','hrrn_whole_gpu'):
            for m in ('p95_wait','large_p95_wait','small_p95_wait','mean_bounded_slowdown','completed_work'):
                failures.append(dict(scenario=s,**paired([r for r in rows if r['scenario']==s],p,m)))
    write_csv(out/'scenario-paired.csv',failures)
    figures(out/'figures',rows,development)
    index={(r['policy'],r['metric']):r for r in summary}
    def value(p,m):return index[p,m]['mean']
    table='\n'.join(f"| {NAMES[p]} | {value(p,'mean_wait'):.3f} | {value(p,'p95_wait'):.3f} | {value(p,'p99_wait'):.3f} | {100*value(p,'long_wait_ratio'):.2f}% | {value(p,'mean_bounded_slowdown'):.3f} | {value(p,'completed_count'):.2f} | {value(p,'completed_work'):.3f} |" for p in ORDER)
    citable='\n'.join(f"| {NAMES[r['comparator']]} | {r['metric']} | {r['difference']:+.4f} | [{r['ci_low']:+.4f}, {r['ci_high']:+.4f}] | [{r['family_ci_low']:+.4f}, {r['family_ci_high']:+.4f}] |" for r in pairs if r['primary'])
    selection=json.loads((Path(development)/'selection.json').read_text(encoding='utf-8'))
    report=f'''# HRRN 第二阶段：独立合成轨迹结果

本地 CPU 离散事件仿真；所有时间和完成工作量都是合成值，不是 GPU 计算性能测量。冻结 RF 与设备硬约束保持不变。EASY 是单受阻 HRRN 队首的时间日历预约，允许预计不推迟预约的回填，包括能在剩余 vGPU 容量中跨预约时刻继续运行的作业。它不是 Slurm 的逐节点原样实现。

开发集为4个独立种子×8条件；12组(H_abs,kappa)仅在开发集选择。选择 H_abs={selection['H_abs']}, kappa={selection['kappa']}，开发保护约束是否通过：{selection['selection_passed_guards']}。未通过时保留最少违反约束的候选，不能追认算法有效。正式输入和评价源码的哈希先封存在freeze.json，再运行测试。

正式集20个新独立种子块，8场景×3种资源/时长关系的随机误差条件，加高负载下3种关系×名义/低估/高估条件，共33条件、660条输入、4620次策略运行。不同误差条件共享到达、资源和实际合成时长。主要统计先在每个种子内等权平均同一场景的条件，再对8场景等权平均，最后对20个种子做配对重采样。高负载的额外误差条件不会增加该场景的总体权重。

等待从ready时间起算，到达停止后全部排空，无TTL。长等待阈值事前固定为8。大作业：按最小设备容量归一化的三维主导请求×GPU数≥1；小作业≤0.4；多GPU独立分层。平均bounded slowdown=max(1,(完成-ready)/max(实际合成时长,1))。P95/P99是每条轨迹的分位数再做宏平均，不是混池后的全局分位数。每条轨迹24个Job，P99接近最大值，需结合20种子波动解释。

| 策略 | 平均等待 | P95 | P99 | 长等待比例 | 平均bounded slowdown | 窗口完成Job数 | 窗口完成合成工作量 |
|---|---:|---:|---:|---:|---:|---:|---:|
{table}

合成工作量=已在固定窗口完整完成Job的 GPU数×核心配额/100×实际合成时长；不代表实际FLOPs。每条轨迹的窗口独立于算法和估计，窗口后继续排空。实际逻辑分配率与受保护空闲率分别报告，不使用两者相加作为真实GPU利用率。EASY未来预约不计入当前空间保护空闲；其当前空闲直接反映在实际分配率中。

以下差值均为细粒度保护减去对照。等待/比例越低越好，工作量越高越好。常规95%区间属于探索性结果；9个主要比较另给Bonferroni校正后的近似配对块bootstrap区间（40000次重采样），不把某个子场景的偶然优势当成总体结论。

| 对照 | 指标 | 配对差值 | 95% CI | 主要比较校正CI |
|---|---|---:|---|---|
{citable}

summary.csv包含均值、种子标准差、最小/最大及95%区间；paired-comparisons.csv包含配对差值；by-scenario.csv、by-condition.csv、scenario-paired.csv提供逐条件结果及失败场景。runs.csv保存全部4620条记录，records保留每个Job的结果和独立审计。details保存事前指定的第一个种子块全事件，用于检查资源保护、预约、回填和估计扩展。

参数敏感性只来自开发集。运行时误差图仅比较高负载下完全配对的名义/0.5倍/2倍/对数随机误差；名义时长等于合成实际值只是诊断，不是现实可获取时长的假设。所有方法得到同一估计；EASY只根据已观察到的仍在运行事件延长预测：新截止=now+max(提交估计,已运行时长,tau)，不读取隐藏完成事件。低估可能造成预约延误，但不允许物理超分配或虚假释放。

固定目标分析记录保护启动时已积累容量比例、固定组合预计等待、所有组合的最早预计等待，以及二者之差。它们只使用当前账本与公开时长预测。估计偏差会影响这些诊断，不能把预测接近凑齐当成真实完成已知。

调度开销以毫秒记录。正式测试并行执行的耗时包含CPU竞争，只作工程诊断；figures/08使用单进程、四个开发场景、暖机后3次重复的独立测量，包含事件记录和保护诊断，不冒充生产调度器延迟。所有策略共用相同的精确缓存适配器；它只缓存冻结接口的确定结果，命中时重建当前版本及保护epoch，不改变特征、标签、评分或候选。

图：01平均等待/P95/P99/完成工作量；02大小与多GPU尾部；03实际分配与空闲保护分别作图；04开发阈值敏感性；05估计误差；06各场景优势与失败；07资源/时长相关性；08串行决策开销。均同时提供PNG和SVG。

边界：单租户同优先级、同节点完整多GPU、有限非抢占作业；CPU和主机内存充足。没有线上Kubernetes/Volcano改动、GPU计算任务、PPO/GNN或真实设备性能测量。结果仅覆盖当前合成生成器、四张GPU和24Job有限轨迹，不能外推成生产规模、公平性或方法新颖性的证据。

EASY时间回填的公开语义参考：[Slurm Scheduling Configuration Guide](https://slurm.schedmd.com/sched_config.html)。本实现使用相同vGPU向量粒度，避免以整节点预约作为弱对照。
'''
    (out/'report.md').write_text(report,encoding='utf-8')
    write_json(out/'analysis-verification.json',dict(success=True,rows=len(rows),seed_blocks=20,conditions=33,
        no_test_tuning=True,freeze_reverified=True,metric_scope='macro mean per-trace statistics, scenario-balanced, paired seed blocks'))
    print('Analysis and figures complete:',out/'report.md',flush=True)

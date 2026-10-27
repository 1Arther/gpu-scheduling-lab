from pathlib import Path
import sys,json,csv,hashlib,math,re
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'.deps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch,FancyArrowPatch
from matplotlib.colors import ListedColormap
from matplotlib.font_manager import FontProperties
import numpy as np
from docx import Document
from docx.shared import Inches,Pt,RGBColor,Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH,WD_BREAK,WD_TAB_ALIGNMENT,WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.table import WD_TABLE_ALIGNMENT,WD_CELL_VERTICAL_ALIGNMENT

ROOT=BASE.parent.parent
OUT=Path('C:/Users/EDY/Desktop/GPU感知分类原理实现与实验学习详解.docx')
FIG=BASE/'figures';FIG.mkdir(exist_ok=True)
plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'font.size':11,'svg.fonttype':'none','axes.spines.top':False,'axes.spines.right':False})
COL=['#315e8a','#ce8b36','#418477','#b66a71','#777c85']
sources=set()
def readcsv(rel):
    p=ROOT/rel;sources.add(p);return list(csv.DictReader(p.open(encoding='utf-8-sig')))
def readjson(rel):
    p=ROOT/rel;sources.add(p);return json.loads(p.read_text(encoding='utf-8-sig'))
def savefig(key,fig):
    fig.savefig(FIG/(key+'.png'),dpi=210,bbox_inches='tight',facecolor='white')
    fig.savefig(FIG/(key+'.svg'),bbox_inches='tight',facecolor='white');plt.close(fig)
def diagram(key,items,subtitle=''):
    # Two-column snake flow, ample padding for readable Chinese labels.
    n=len(items);rows=math.ceil(n/2)
    fig,ax=plt.subplots(figsize=(9,rows*1.32+.45));ax.set_xlim(0,10);ax.set_ylim(0,rows*1.5);ax.axis('off')
    positions=[]
    for i,(head,body) in enumerate(items):
        row=i//2;col=i%2 if row%2==0 else 1-i%2
        x=.15+col*5.05;y=rows*1.5-(row+1)*1.5+.22;w=4.45;h=1.05
        ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=.04,rounding_size=.08',edgecolor=COL[i%3],facecolor='#f3f6f8',linewidth=1.2))
        ax.text(x+.18,y+.75,f'{i+1:02d}  {head}',fontsize=12,fontweight='bold',va='center')
        ax.text(x+.18,y+.35,body,fontsize=10,va='center',linespacing=1.6)
        positions.append((x,y,w,h))
    for i in range(n-1):
        if key=='resource_domains':continue
        x,y,w,h=positions[i];a,b,c,d=positions[i+1]
        if abs(y-b)<.1:
            start=(x+w+.05,y+h/2) if a>x else (x-.05,y+h/2)
            end=(a-.08,b+d/2) if a>x else (a+c+.08,b+d/2)
        else:start=(x+w/2,y-.04);end=(a+c/2,b+d+.04)
        ax.add_patch(FancyArrowPatch(start,end,arrowstyle='-|>',mutation_scale=15,color='#4b5563',linewidth=1.2))
    savefig(key,fig)

def charts():
    diagram('roadmap',[('研究定义','GPU当前状态是否满足参考负载'),('硬件与动态特征','公开数值画像＋逻辑剩余配额'),('标签与模型','确定性监督 → 双目标随机森林'),('评价与边界','新状态／新型号／相关验证覆盖'),('Pod设备排序','硬约束过滤 → RF与启发式评分'),('原生分配与复核','UUID → Allocate → 释放后重分类')])
    diagram('resource_domains',[('公开规格域','标称容量、带宽、算力；来源可追溯'),('模型参考域','旧画像容量与显式剩余比例映射'),('调度逻辑域','注册总量 − 已预留的Pod配额'),('物理运行域','CUDA实际申请与宿主机进程占用')])
    diagram('data_pipeline',[('空资源账本','固定硬件画像；剩余资源初始化'),('合法分配或释放','显存／算力／实例约束始终有效'),('保存状态样本','动态特征、episode、特征哈希'),('确定性理论监督','分别计算训练和推理参考适配'),('按轨迹或型号分组','训练／验证／测试互不泄漏'),('训练RF并评价','预测不作为新真值回灌')])
    diagram('forest',[('训练输入','数值特征 → 训练集拟合缺失处理'),('双列监督目标','训练是否适配／推理是否适配'),('120棵随机树','同一个多输出随机森林'),('两个边际估计','pT与pI；各自按0.5判断'),('组合四类','训练优先／推理优先／一体／不足'),('适用性检查','必要参数、数值范围、相关验证')])
    diagram('scoring',[('读取Pod声明','training / inference；具体资源申请'),('原生硬约束过滤','显存、配额、实例、后端和共享模式'),('模型与状态检查','原始RF结果＋provisional / pending'),('计算可解释得分','学习项＋硬件共享＋装箱＋平衡'),('同节点完整方案','每份申请落实到明确UUID'),('原生提交前复核','当前账本有效才允许分配')])
    diagram('integration',[('FilterNode与ScoreNode','原生资源检查；复制候选逻辑快照'),('Go发起HTTP','释放账本锁；携带请求与快照摘要'),('常驻Python RF','加载一次；返回分类、分数、UUID'),('allocateRF','重新检查快照、完整方案与硬约束'),('commitNativePlan','原生AddPod记账；写设备注解'),('kubelet与Device Plugin','消费UUID；配置容器；随后可释放')])
    diagram('simulation_arch',[('同一到达轨迹','相同Pod、种子、资源与观察窗'),('三个调度策略','Binpack／静态RF／动态RF'),('逻辑资源事件','分配 → 运行中 → 完成 → 释放'),('独立合成执行器','能力、配额、共享、隐藏工作量'),('统一指标','等待、JCT、批Pod响应、预留率'),('配对比较','不把分类器答案作为执行器答案')])
    q=np.linspace(.05,1,120);F=82.6;B=1008
    fig,axes=plt.subplots(1,2,figsize=(9,3.4))
    for ax,w,d,limit,title in zip(axes,[2,.06],[8,2.5],[.2,.01],['训练参考工作单元','推理参考请求']):
        t=np.maximum(w/(.35*F*q),d/(.55*B*q));ax.plot(q*100,t*1000,color=COL[0],label='理论瓶颈时间')
        ax.axhline(limit*1000,color=COL[3],ls='--',label='合同时间门槛');ax.axvline(40,color='#999',ls=':',lw=1)
        ax.set(title=title,xlabel='剩余逻辑算力配额',ylabel='理论时间 / ms');ax.set_ylim(0,450 if w==2 else 65);ax.grid(alpha=.18);ax.legend(fontsize=8)
    fig.tight_layout();savefig('theory_times',fig)
    fig,axes=plt.subplots(3,1,figsize=(9,4.9))
    for ax,ttl,labels in zip(axes,['已知型号新状态','完全未见型号','整个架构留出'],[['4090轨迹A','4090轨迹B','4090轨迹C'],['4090等型号','L40等型号','新型号'],['Ampere / Ada','Hopper等验证','Blackwell']]):
        for j,lab in enumerate(labels):
            ax.barh(0,1,left=j,color=COL[j],height=.6);ax.text(j+.5,0,lab,ha='center',va='center',color='white',fontsize=10)
            ax.text(j+.5,.48,['训练','验证','测试'][j],ha='center',fontsize=10)
        ax.set(xlim=(0,3),ylim=(-.5,.85));ax.axis('off');ax.text(-.03,.85,ttl,transform=ax.transAxes,fontweight='bold')
    fig.tight_layout(h_pad=1);savefig('split_protocols',fig)
    names=['训练优先','推理优先','训推一体','参考不足'];fig,axes=plt.subplots(1,2,figsize=(9,4))
    for ax,protocol,title in zip(axes,['known_states','A_ada'],['已知型号新状态','Ada未见型号']):
        m=readjson(f'results/offline-v4/{protocol}/metrics.json')['selected_metrics']['test'];cm=np.array(m['confusion_matrix_true_rows_predicted_columns'])
        ax.imshow(cm,cmap='Blues');ax.set_xticks(range(4),names,rotation=35,ha='right',fontsize=9);ax.set_yticks(range(4),names,fontsize=9);ax.set(title=title,xlabel='预测类别',ylabel='真实类别')
        for i in range(4):
            for j in range(4):ax.text(j,i,str(cm[i,j]),ha='center',va='center',color='white' if cm[i,j]>cm.max()/2 else '#111')
    fig.tight_layout();savefig('confusion',fig)
    rows=[r for r in readcsv('results/round8-20261020/classification-all-splits.csv') if r['round']=='4' and r['split']=='test']
    labs={'known_states':'已知状态','A_ampere':'Ampere型号','A_ada':'Ada型号','B_multi_architecture':'多架构型号','C_blackwell_holdout':'整架构留出','D_a10_diagnostic':'A10诊断'}
    fig,ax=plt.subplots(figsize=(9,3.7));x=np.arange(len(rows));ax.bar(x-.18,[float(r['accuracy'])*100 for r in rows],.35,label='原始RF准确率',color=COL[0]);ax.bar(x+.18,[float(r['accepted_test_fraction'])*100 for r in rows],.35,label='暂定输出覆盖',color=COL[2]);ax.set_xticks(x,[labs[r['protocol']] for r in rows],rotation=15);ax.set(ylabel='百分比 / %',ylim=(0,112));ax.legend(ncol=2,fontsize=9);ax.grid(axis='y',alpha=.15);fig.tight_layout();savefig('classification',fig)
    ab=[r for r in readcsv('results/round8-20261020/feature-ablations.csv') if r['protocol']=='B_multi_architecture']
    alias=['基础14列','仅绝对值','相对显存','去静态容量','有效FP32','有效带宽','二者共同','再加比例']
    fig,ax=plt.subplots(figsize=(9,3.7));x=np.arange(len(ab));ax.plot(x,[float(r['validation_accuracy'])*100 for r in ab],'o-',label='验证集',color=COL[1]);ax.plot(x,[float(r['test_accuracy'])*100 for r in ab],'s-',label='测试集',color=COL[0]);ax.set_xticks(x,alias,rotation=25);ax.set(ylabel='合成标签准确率 / %',ylim=(65,102));ax.grid(alpha=.18);ax.legend();fig.tight_layout();savefig('ablation_features',fig)
    cr=readcsv('results/rf-ablation-20261019/linux-final/candidate-scores.csv');w=[r for r in cr if r['case_id']=='within-l40-0-inference' and r['strategy']=='rf_on'];w.sort(key=lambda r:r['gpu_uuid'])
    fig,ax=plt.subplots(figsize=(8.5,3.6));x=np.arange(3);h=[float(r['heuristic_contribution']) for r in w];a=[float(r['rf_contribution']) for r in w];ax.bar(x-.2,h,.36,label='RF off：启发式',color='#aeb7c0');ax.bar(x+.2,h,.36,color=COL[0],label='RF on：启发式');ax.bar(x+.2,a,.36,bottom=h,color=COL[1],label='RF学习贡献');ax.set_xticks(x,['L40 GPU-00','L40 GPU-01','L40 GPU-02']);ax.set(ylabel='Go协议评分 / 100',ylim=(0,70));ax.legend(fontsize=9);ax.grid(axis='y',alpha=.15);fig.tight_layout();savefig('witness',fig)
    st=readcsv('results/round8-live-20261020/verified/states.csv');fig,axes=plt.subplots(2,1,figsize=(9,5),sharex=True);x=np.arange(5)
    axes[0].plot(x,[int(r['free_memory_mib']) for r in st],'o-',color=COL[0]);axes[0].set_ylabel('逻辑剩余显存 / MiB');axes[0].set_ylim(0,28500)
    axes[1].plot(x,[int(r['free_compute_quota']) for r in st],'s-',color=COL[2]);axes[1].set_ylabel('剩余算力配额');axes[1].set_ylim(0,120)
    for ax,key in zip(axes,['free_memory_mib','free_compute_quota']):
        for i,r in enumerate(st):ax.annotate(r[key],(i,int(r[key])),xytext=(0,8),textcoords='offset points',ha='center',fontsize=10)
        ax.grid(alpha=.18)
    axes[1].set_xticks(x,['S0\n训推一体','S1\n训练优先','S2\n参考不足','S3\n推理优先','S4\n训推一体']);fig.tight_layout();savefig('dynamic',fig)
    sim=readcsv('results/round8-20261020/simulation-main.csv');sc=['steady_mixed','bursty_mixed','training_heavy','memory_heavy']
    actual={r['scenario'] for r in sim};sc=[s for s in sc if s in actual]+sorted(actual-set(sc))
    zh={'steady_mixed':'平稳混合','bursty_mixed':'突发混合','training_heavy':'训练为主','memory_heavy':'显存密集','training_dominant':'训练为主','memory_pressure':'显存密集'}
    fig,axes=plt.subplots(1,2,figsize=(9,3.8))
    for ax,metric,title in zip(axes,['mean_wait_s','training_mean_jct_s'],['平均等待','训练Pod完成时间']):
        for j,strategy in enumerate(['binpack','static_rf','dynamic_rf']):
            v=[next(r for r in sim if r['scenario']==s and r['strategy']==strategy) for s in sc]
            ax.bar(np.arange(len(sc))+(j-1)*.24,[float(r[metric+'_mean']) for r in v],.23,yerr=[float(r[metric+'_std']) for r in v],capsize=2,label=['Binpack','静态RF','动态RF'][j],color=COL[j])
        ax.set_xticks(np.arange(len(sc)),[zh.get(s,s) for s in sc],rotation=20);ax.set(title=title,ylabel='合成秒');ax.grid(axis='y',alpha=.15)
    axes[0].legend(fontsize=8);fig.tight_layout();savefig('simulation',fig)

def tables():
    T={}
    T['evidence']=(['证据类型','来源例子','回答的问题'],[['公开规格','厂商文档与参数来源表','硬件公开能力是什么'],['合成监督','参考合同＋合法状态轨迹','理论规则如何产生类别'],['分类测试','已知状态／型号留出','RF是否拟合目标映射'],['调度仿真','离散事件与独立执行器','给定假设下策略如何表现'],['原生Go离线','fake-client＋真实RF调用','学习项是否影响最终UUID'],['真实Kubernetes','sleep Pod与插件响应','分配和释放链路是否成立']])
    hw=readcsv('results/offline-v4/nvidia-hardware.csv');T['hardware']=(['型号或配置','架构','参考MiB','GB/s','FP32 TFLOPS','W'],[[r['model'].replace('GeForce ','').replace('standard ','').replace('reference',''),r['architecture'].replace(' Lovelace',''),r['memory_capacity_mib'],r['memory_bandwidth_gbps'],r['fp32_tflops'],r['max_board_power_w']] for r in hw])
    T['features']=(['层次','字段或组合','含义'],[['静态7项','容量、带宽、FP32、FP16、BF16、INT8、功耗','低精度口径不一致者在模型中屏蔽'],['动态5项','剩余MiB、剩余配额、实例数、训练数、推理数','来自逻辑资源快照'],['比例2项','剩余显存比例、剩余算力比例','支持跨容量状态表达'],['默认派生1项','effective_fp32_tflops','FP32 × 配额比例'],['身份元数据','UUID、型号、厂商、架构','绑定、审计、验证路由；非RF数值输入']])
    T['contract']=(['要求','训练参考','推理参考'],[['计算精度','普通dense FP32','普通dense FP32'],['最低显存','8192 MiB','4096 MiB'],['最低算力配额','20','10'],['每单位计算量','2.0 TFLOP','0.06 TFLOP'],['每单位内存流量','8.0 GB','2.5 GB'],['理论时间上限','0.2 s','0.01 s'],['最低参考吞吐','5/s','100/s'],['数据性质','声明的合成合同','声明的合成合同']])
    T['classes']=(['训练判断','推理判断','展示类别','理解方式'],[['是','是','训推一体','两种参考分别满足'],['是','否','训练优先','仅训练参考满足'],['否','是','推理优先','仅推理参考满足'],['否','否','参考负载不足','不阻止更小Pod通过硬过滤'],['未知或依据不足','未知或依据不足','信息不足 待验证','保留原始RF诊断；排序可回退']])
    plan=readjson('data/round4-plan.json');labels={'known_states':'已知新状态','A_ampere':'同架构Ampere','A_ada':'同架构Ada','B_multi_architecture':'多架构未知型号','C_blackwell_holdout':'整个Blackwell留出','D_a10_diagnostic':'A10诊断'}
    T['splits']=(['协议','训练与验证原则','测试型号或范围'],[[labels[k], '按episode划分；排除三款全新型号' if k=='known_states' else ('训练：'+', '.join(v['train'])+'；验证：'+', '.join(v['validation'])), '已知12型号的全新episode' if k=='known_states' else ', '.join(v['test'])] for k,v in plan['protocols'].items()])
    rows=[r for r in readcsv('results/round8-20261020/classification-all-splits.csv') if r['round']=='4' and r['split']=='test']
    T['metrics']=(['协议','N','Accuracy','四类F1','出现类F1','覆盖率'],[[labels[r['protocol']],r['samples'],f"{float(r['accuracy'])*100:.2f}%",f"{float(r['macro_f1_fixed_four']):.4f}",f"{float(r['macro_f1_observed']):.4f}",f"{float(r['accepted_test_fraction'])*100:.2f}%"] for r in rows])
    T['witness']=(['同节点L40','原始类别','启发式','RF贡献','开启总分','off / on选择'],[['GPU-00','训推一体','26.412','30.000','56.412','否 / 是'],['GPU-01','参考不足','28.827','0','28.827','否 / 否'],['GPU-02','训练优先','29.427','0','29.427','是 / 否']])
    st=readcsv('results/round8-live-20261020/verified/states.csv');cn={'balanced':'训推一体','training_preferred':'训练优先','inference_preferred':'推理优先','unavailable':'参考不足'}
    T['states']=(['状态','剩余MiB','算力','实例','训练 / 推理','实际RF类别'],[[r['state'],r['free_memory_mib'],r['free_compute_quota'],r['allocated_vgpu_count'],('是' if r['training_adapted']=='True' else '否')+' / '+('是' if r['inference_adapted']=='True' else '否'),cn[r['label']]] for r in st])
    trows=[]
    for r in st:
        q=int(r['free_compute_quota'])/100;t=max(2/(82.6*.35*q),8/(1008*.55*q));i=max(.06/(82.6*.35*q),2.5/(1008*.55*q))
        trows.append([r['state'],str(int(r['free_memory_mib'])*22888//24564),f'{t:.5f}',f'{i:.5f}','理论计算；实测RF类别见上表'])
    T['state_theory']=(['状态','模型参考MiB','训练理论s','推理理论s','性质'],trows)
    alloc=readcsv('results/round8-live-20261020/verified/allocations.csv')
    # Field names are retained in an extracted source snapshot for audit.
    (BASE/'allocation-columns.json').write_text(json.dumps(list(alloc[0]),ensure_ascii=False),encoding='utf-8')
    T['allocations']=(['分配对象','数量','UUID证据','释放结果'],[['blocker-0 至 blocker-2','3','分别占据另三张卡；RF / 原生 / 插件一致','全部释放'],['compute 与 memory','2','同一目标UUID；三方一致','按顺序释放'],['observer-0 至 observer-2','3','同一目标UUID；三方一致','取证后逐个释放'],['完整成功窗口','8','三方一致8/8；原生配额增量正确8/8','最终逻辑预留归零']])
    T['source_map']=(['源文件','核心函数或对象','学习重点'],[['crossgpu/catalog.py','compile_catalog','来源、单位、可比性屏蔽'],['crossgpu/theory.py','assess / hard_checks / combine','标签公式与两个目标'],['crossgpu/data.py','generate / split_known / audit','轨迹生成与防泄漏'],['nvidia/features.py','FEATURE_SETS / enrich','输入交互特征'],['nvidia/model.py','select_models / raw_predict / predict_device','RF、验证选择、适用性'],['runtime/schema.py','gpu_errors / model_state / apply_slot','容量映射与状态更新'],['runtime/service.py','classify_gpu / hard_filter / _score / rank_gpus','统一API和解释性评分'],['runtime/volcano.py','HTTP协议适配','原生快照映射与响应'],['vgpu/device_info.go','FilterNode / ScoreNode / Allocate / SubResource','原生分配生命周期'],['vgpu/rf_job.go','rfPlanFor / allocateRF / commitNativePlan','建议进入最终UUID'],['vgpu/hamicore.go','TryAddPod / AddPod / SubPod','原生逻辑记账']])
    T['symbols']=(['符号','含义','单位或范围'],[['M_registered','原生注册逻辑显存总量','MiB'],['M_catalog','冻结目录参考显存','MiB'],['M_free','逻辑剩余显存','MiB'],['Q_free / q','剩余算力额度 / 比例','0—100 / 0—1'],['F / B','常规FP32峰值 / 内存带宽','TFLOPS / GB/s'],['W / D','参考计算量 / 内存流量','TFLOP / GB'],['pT / pI','两个目标的RF边际估计','0—1；不是可靠性'],['A / H / P / B_score','适配、硬件共享、装箱、平衡项','策略分数'],['TP / FP / FN','真阳性、假阳性、假阴性','按某一类别统计']])
    manifest=readjson('models/pod-gpu-rf-v1/manifest.json')
    T['feature_order']=(['顺序','特征名','说明'],[[str(i+1),k,'当前统一口径屏蔽' if i in [3,4,5] else '静态' if i<7 else '动态' if i<12 else '派生'] for i,k in enumerate(manifest['feature_order'])])
    T['evidence_paths']=(['材料','项目相对路径'],[['冻结模型与合同','models/pod-gpu-rf-v1/'],['第四轮计划','data/round4-plan.json'],['第四轮数据与协议','results/offline-v4/'],['分类与特征汇总','results/round8-20261020/classification-all-splits.csv；feature-ablations.csv'],['RF独立贡献','results/rf-ablation-20261019/linux-final/'],['第二轮仿真','results/offline-v2/；results/round8-20261020/simulation-main.csv'],['真实第七轮','results/round7-live-window-20261019/verified/'],['真实第八轮','results/round8-live-20261020/verified/'],['最终真实报告','docs/round8-real-kubernetes-report.md'],['完整集成协议','docs/rf-job-protocol-v1.md']])
    T['glossary']=(['术语','学习解释'],[['GPU画像','设备数值能力与来源的统一记录'],['参考合同','明确负载需求、工作量和门槛的分类标尺'],['synthetic','按声明规则生成；非真实性能测量'],['episode','一段合法分配释放状态轨迹'],['型号留出','某型号全部状态不进入训练和验证'],['Macro-F1','先按类算F1，再等权平均'],['provisional','当前验证条件下的暂定输出'],['pending_verification','信息或证据不足；保留原始诊断'],['rf-job','本项目新增的RF设备选择策略名'],['UUID','物理GPU身份，不是性能特征'],['Allocate','需区分scheduler提交与插件设备配置'],['账本','逻辑申请与剩余配额的权威记录'],['fake-client','替代API Server交互的测试客户端'],['消融','保持其他条件不变，移除一个因素'],['JCT','任务到达至完成的时间'],['逻辑预留率','已预留配额随时间的积分比例']])
    raw=readjson('models/pod-gpu-rf-v1/source-records.json')
    T['official_sources']=(['来源编号与设备','官方URL与定位'],[[k, v['url']+'\n'+v['locator']] for k,v in raw['sources'].items()])
    return T

def setfont(style,face,size,bold=False):
    style.font.name='Times New Roman';style.font.size=Pt(size);style.font.bold=bold;style.font.italic=False;style.font.color.rgb=RGBColor(0,0,0)
    rp=style.element.get_or_add_rPr();rf=rp.rFonts;rf.set(qn('w:eastAsia'),face)
    for key in ['asciiTheme','hAnsiTheme','eastAsiaTheme','cstheme']:rf.attrib.pop(qn('w:'+key),None)
    for x in style.element.xpath('./w:pPr/w:pBdr | ./w:rPr/w:iCs | ./w:rPr/w:bCs | ./w:rPr/w:spacing'):x.getparent().remove(x)
def field(p,instr,text='1'):
    f=OxmlElement('w:fldSimple');f.set(qn('w:instr'),instr);r=OxmlElement('w:r');t=OxmlElement('w:t');t.text=text;r.append(t);f.append(r);p._p.append(f)
def eq(p,text):
    om=OxmlElement('m:oMathPara');o=OxmlElement('m:oMath');om.append(o)
    # Native Office Math; explicit fractions for ratios where appropriate.
    def mr(parent,txt):
        r=OxmlElement('m:r');t=OxmlElement('m:t');t.text=txt;r.append(t);parent.append(r)
    def frac(a,b):
        f=OxmlElement('m:f');n=OxmlElement('m:num');d=OxmlElement('m:den');mr(n,a);mr(d,b);f.extend([n,d]);o.append(f)
    if text.startswith('T_ref'):
        mr(o,'T_ref = max(');frac('W','0.35 × F × q');mr(o,',  ');frac('D','0.55 × B × q');mr(o,')')
    elif text.startswith('F1 ='):mr(o,'F1 = ');frac('2 × Precision × Recall','Precision + Recall')
    else:mr(o,text)
    p._p.append(om);p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.line_spacing=1.15;p.paragraph_format.first_line_indent=Pt(0)

def build():
    T=tables();d=Document();sec=d.sections[0];sec.page_width=Cm(21);sec.page_height=Cm(29.7)
    sec.top_margin=Cm(2.25);sec.bottom_margin=Cm(2.1);sec.left_margin=Cm(2.35);sec.right_margin=Cm(2.35);sec.header_distance=Cm(1);sec.footer_distance=Cm(1)
    for name,face,size,bold in [('Normal','宋体',11.5,False),('Title','黑体',25,False),('Subtitle','宋体',13,False),('Heading 1','黑体',18,False),('Heading 2','黑体',13,False),('Caption','宋体',9.5,False)]:setfont(d.styles[name],face,size,bold)
    s=d.styles['Normal'].paragraph_format;s.line_spacing=Pt(17.5);s.space_after=Pt(3);s.first_line_indent=Pt(23);s.widow_control=True
    for name in ['Heading 1','Heading 2']:
        f=d.styles[name].paragraph_format;f.first_line_indent=Pt(0);f.keep_with_next=True;f.keep_together=True;f.space_after=Pt(6);f.space_before=Pt(8);f.line_spacing=1.15
    d.styles['Heading 1'].paragraph_format.page_break_before=False
    d.styles['Heading 1'].paragraph_format.space_before=Pt(22)
    d.styles['Caption'].paragraph_format.first_line_indent=Pt(0)
    sec.different_first_page_header_footer=True
    h=sec.header.paragraphs[0];h.text='GPU 感知分类原理 实现与实验学习详解';h.alignment=WD_ALIGN_PARAGRAPH.RIGHT
    h.paragraph_format.first_line_indent=Pt(0)
    for r in h.runs:r.font.size=Pt(8);r.font.color.rgb=RGBColor.from_string('666666')
    f=sec.footer.paragraphs[0];f.alignment=WD_ALIGN_PARAGRAPH.CENTER;f.paragraph_format.first_line_indent=Pt(0);field(f,' PAGE ')
    p=d.add_paragraph();p.paragraph_format.space_after=Pt(70)
    p=d.add_paragraph('GPU感知分类原理\n实现与实验学习详解','Title');p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_after=Pt(28)
    p=d.add_paragraph('从硬件画像与参考负载\n到随机森林动态适配和Volcano设备分配','Subtitle');p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.line_spacing=Pt(24)
    p=d.add_paragraph('基于 gpu-scheduling-lab 第一至第八轮项目记录');p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_before=Pt(55)
    for txt in ['学习讲义 · 算法推导 · 源码导读 · 实验复盘','包含21章正文、5个附录和15幅图','整理日期  2026年10月26日']:
        p=d.add_paragraph(txt);p.paragraph_format.first_line_indent=Pt(0)
    p=d.add_paragraph('阅读目标：能够解释每一个分类结果的来源，并沿着代码与证据追踪它如何影响最终GPU分配。');p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_before=Pt(38)
    content=(BASE/'book.txt').read_text(encoding='utf-8').splitlines();chapters=[x[2:] for x in content if x.startswith('# ')]
    pages=json.loads((BASE/'page-map.json').read_text(encoding='utf-8')) if (BASE/'page-map.json').exists() else {}
    d.add_heading('目录',level=1).paragraph_format.page_break_before=True
    for title in chapters:
        p=d.add_paragraph();p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_after=Pt(2);p.paragraph_format.line_spacing=Pt(15)
        p.paragraph_format.tab_stops.add_tab_stop(Cm(16.1),WD_TAB_ALIGNMENT.RIGHT,WD_TAB_LEADER.DOTS)
        p.add_run(title+'\t'+str(pages.get(title,'—'))).font.size=Pt(10)
    p=d.add_paragraph('注：正文数据为历史实验记录；图表分别注明合成、原生离线或真实逻辑实验。');p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_before=Pt(12)
    table_count=0;figcount=0;code=False
    for line in content:
        if not line.strip():continue
        if line.startswith('# '):
            p=d.add_heading(line[2:],1)
            if line[2:] in ['阅读说明','附录A 关键公式与符号速查','参考资料']:
                p.paragraph_format.page_break_before=True
        elif line.startswith('## '):d.add_heading(line[3:],2)
        elif line=='@CODE':code=True
        elif line=='@ENDCODE':code=False
        elif code:
            p=d.add_paragraph();p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.left_indent=Pt(8);p.paragraph_format.line_spacing=Pt(12);p.paragraph_format.space_after=Pt(0);p.paragraph_format.keep_with_next=True
            r=p.add_run(line);r.font.name='Consolas';r.font.size=Pt(8)
        elif line.startswith('@EQ '):eq(d.add_paragraph(),line[4:])
        elif line.startswith('@FIG '):
            key,caption,note=line[5:].split('|');p=d.add_paragraph();p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.keep_with_next=True;p.paragraph_format.line_spacing=1.0;p.alignment=WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(str(FIG/(key+'.png')),width=Cm(16))
            p=d.add_paragraph(caption,'Caption');p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.keep_with_next=True
            p=d.add_paragraph(note,'Caption');p.paragraph_format.space_after=Pt(10);figcount+=1
        elif line.startswith('@TABLE '):
            key=line[7:];heads,rows=T[key];table_count+=1
            p=d.add_paragraph('表'+str(table_count)+'  '+{'evidence':'证据层次','hardware':'第四轮NVIDIA硬件范围','features':'特征层次','contract':'冻结参考负载合同','classes':'两个目标与综合类别','splits':'按型号或轨迹划分的协议','metrics':'第四轮分类测试汇总','witness':'L40同节点RF开关案例','states':'真实4090五状态','state_theory':'同一状态的理论解释','allocations':'真实第八轮分配核对','source_map':'源码阅读索引','symbols':'符号与单位','feature_order':'默认输入顺序','evidence_paths':'证据文件索引','glossary':'术语速查','official_sources':'已保存的官方参数来源'}[key],'Caption');p.paragraph_format.keep_with_next=True
            table=d.add_table(rows=1,cols=len(heads));table.alignment=WD_TABLE_ALIGNMENT.CENTER;table.autofit=False
            widths={'hardware':[4.6,2.1,2,2,3,1.2],'features':[2,6.7,6.8],'splits':[2.5,9.2,4.3],'source_map':[5,5,6],'feature_order':[1.2,10,4.8],'official_sources':[3.3,12.7],'evidence_paths':[3.2,12.8],'glossary':[4,12]}.get(key,[16/len(heads)]*len(heads))
            factor=16/sum(widths);widths=[x*factor for x in widths]
            for col,w in zip(table.columns,widths):col.width=Cm(w)
            allrows=[heads]+rows
            for ir,vals in enumerate(allrows):
                row=table.rows[0] if ir==0 else table.add_row()
                row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
                if ir==0:row._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
                for j,(cell,txt) in enumerate(zip(row.cells,vals)):
                    cell.width=Cm(widths[j]);cell.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    p=cell.paragraphs[0];p.alignment=WD_ALIGN_PARAGRAPH.LEFT;p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.line_spacing=Pt(11.5 if key=='official_sources' else 13);p.paragraph_format.space_after=Pt(0);p.paragraph_format.keep_with_next=False
                    if key in ['hardware','metrics','states','witness','state_theory'] and j>0:p.alignment=WD_ALIGN_PARAGRAPH.CENTER
                    # Keep identifiers literal; Word can wrap long runs within fixed cells.
                    txt=str(txt)
                    r=p.add_run(txt);r.font.size=Pt(9 if key in ['official_sources','splits','hardware'] else 9.5);r.bold=ir==0
                    pr=cell._tc.get_or_add_tcPr();mar=OxmlElement('w:tcMar')
                    for side,v in [('top',45 if key=='official_sources' else 65),('bottom',45 if key=='official_sources' else 65),('left',65),('right',65)]:
                        el=OxmlElement('w:'+side);el.set(qn('w:w'),str(v));el.set(qn('w:type'),'dxa');mar.append(el)
                    pr.append(mar);bd=OxmlElement('w:tcBorders')
                    for side in ['top','bottom','left','right']:
                        el=OxmlElement('w:'+side);el.set(qn('w:val'),'single');el.set(qn('w:sz'),'4');el.set(qn('w:color'),'D9D9D9');bd.append(el)
                    pr.append(bd)
                    if ir==0:
                        sh=OxmlElement('w:shd');sh.set(qn('w:fill'),'E9EEF2');pr.append(sh)
            p=d.add_paragraph();p.paragraph_format.space_after=Pt(1);p.paragraph_format.line_spacing=Pt(3);p.paragraph_format.first_line_indent=Pt(0)
        else:
            p=d.add_paragraph(line)
            if line.startswith('['):
                p.paragraph_format.first_line_indent=Pt(0)
                for r in p.runs:r.font.size=Pt(10)
    d.core_properties.title='GPU感知分类原理实现与实验学习详解';d.core_properties.subject='基于现有项目源码和实验记录的学习讲义';d.core_properties.author='gpu-scheduling-lab'
    d.save(OUT)
    for rel in ['gpu_lab/crossgpu/theory.py','gpu_lab/crossgpu/catalog.py','gpu_lab/crossgpu/data.py','gpu_lab/nvidia/model.py','gpu_lab/nvidia/features.py','gpu_lab/runtime/service.py','gpu_lab/runtime/schema.py','docs/rf-job-protocol-v1.md','docs/round8-real-kubernetes-report.md']:sources.add(ROOT/rel)
    audit={'output':str(OUT),'source_characters':len('\n'.join(content)),'chapter_count':len(chapters),'figure_count':figcount,'table_count':table_count,'source_files':[{'path':str(p.relative_to(ROOT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(sources)],'new_model_training':False,'cluster_operations':False}
    (BASE/'build-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in audit.items() if k!='source_files'},ensure_ascii=False))

if __name__=='__main__':
    if '--doc-only' not in sys.argv:charts()
    build()

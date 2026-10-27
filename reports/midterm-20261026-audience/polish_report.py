from pathlib import Path
from zipfile import ZipFile
from copy import deepcopy
from lxml import etree
import json,hashlib,shutil

HERE=Path(__file__).resolve().parent
SOURCE=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_修订版.docx')
OUTPUT=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_汇报版.docx')
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W='{'+NS['w']+'}'
TITLE='异构vGPU集群的性能感知与公平调度设计与实现'

def sha(data):return hashlib.sha256(data).hexdigest()

def text(p):return ''.join(p.itertext())

def put(p,s):
    ts=p.findall('.//w:t',NS)
    assert len(ts)==1,len(ts)
    ts[0].text=s
    return p

def rebuild(cell,items,patterns):
    for c in list(cell):
        if c.tag!=W+'tcPr':cell.remove(c)
    for role,s in items:
        cell.append(put(deepcopy(patterns[role]),s))

def main():
    raw=SOURCE.read_bytes()
    retained=HERE/'中期报告_汇报润色前.docx'
    if not retained.exists():shutil.copy2(SOURCE,retained)
    assert retained.read_bytes()==raw
    with ZipFile(SOURCE) as z:
        root=etree.fromstring(z.read('word/document.xml'))
        body=root.find('w:body',NS)
        tables=body.findall('w:tbl',NS)
        abstract=tables[0].findall('w:tr',NS)[6].findall('w:tc',NS)[2]
        abstract_text=('针对异构vGPU集群中任务需求与设备能力难以匹配、共享后资源适配性持续变化的问题，本研究构建GPU静态硬件画像与动态逻辑资源联合描述，采用随机森林预测参考训练、推理负载的适配性，并实现面向Pod任务类型的设备选择策略。目前已完成硬件参数库、分类模型、调度仿真及Volcano集成。180组配对离线测试中，RF学习项使37组最终设备选择发生变化；真实Kubernetes实验验证了设备UUID一致性、逻辑配额分配与释放，并观察到同一GPU在四种适配类别间动态切换。分类与仿真采用合成参考负载，真实集群实验验证逻辑分配。后续将完善公平性评价与论文实验分析。')
        put(abstract.find('w:p',NS),abstract_text)

        cell=tables[1].find('w:tr/w:tc',NS)
        ps=cell.findall('w:p',NS)
        patterns={'head':ps[0],'sub':ps[1],'body':ps[2]}
        dynamic_table=deepcopy(cell.find('w:tbl',NS))
        before=[
            ('head','（一）总体进展情况'),
            ('sub','1. 研究目标与总体进展'),
            ('body','本课题针对异构vGPU集群中设备能力差异、任务需求不匹配及共享后适配性变化等问题，研究资源能力感知与任务调度方法。目前已打通“硬件画像—动态适配分类—任务感知评分—设备分配—资源释放”的完整流程，形成可运行的Volcano调度原型，并完成分类评价、仿真对比、学习项消融和真实集群验证。'),
            ('body','围绕开题确定的研究目标，现阶段重点完成GPU能力描述、动态分类及设备级调度。结合公开数据的可获取性，采用具有明确参考负载依据的随机森林进行适配判断；在此基础上，为后续引入等待时间补偿与公平性约束提供资源状态和调度接口。'),
            ('sub','2. GPU画像与动态适配分类方法'),
            ('body','已整理7个厂商22款设备的公开参数及来源，并针对NVIDIA核实15种硬件配置。硬件画像覆盖显存容量、带宽、不同精度理论算力和板卡功耗，保留参数单位与计算口径，为异构设备提供统一的数值描述。对于未公开参数保留缺失标记，分类输入以可核实、可比较的能力特征为主。'),
            ('body','在静态画像基础上，引入剩余逻辑显存、剩余算力配额、已分配vGPU实例数及训练、推理任务组成，并构造资源比例和有效算力特征。模型分别预测参考训练与推理负载的适配性，组合形成训练优先、推理优先、训推一体及参考负载不足四类；输入信息或验证覆盖不足时采用保守处理。每次资源分配或释放后重新分类，使设备适配性随当前可用配额更新。'),
            ('body','分类实验基于公开硬件规格与合成参考负载构建监督标签，调度仿真使用独立执行时间模型比较策略，真实集群使用sleep Pod验证设备选择及逻辑分配。三类实验分别评价模型分类效果、调度行为与系统可运行性，形成相互补充的验证体系。'),
            ('sub','3. 分类模型与跨型号测试结果'),
            ('body','已构建7710个合成资源状态，保留独立训练、验证和测试划分。已知型号的新资源状态测试使用3667个训练样本、1240个验证样本和1244个测试样本；跨型号实验按GPU型号分组留出，避免同型号资源状态同时进入训练和测试。模型预处理与参数选择分别在训练、验证阶段完成。'),
            ('body','在合成参考标签上，已知型号新状态测试准确率为100.00%，宏平均F1为1.0000；同架构未见RTX 6000 Ada测试准确率为96.70%，宏平均F1为0.9547。宏平均F1对四类分类效果等权评价，能够同时关注不同类别的识别情况。结果表明，该模型能够较好表达参考负载下的资源适配关系，并对未见型号给出初步判断。'),
            ('sub','4. 任务感知调度与Volcano系统实现'),
            ('body','用户通过Pod标签声明训练或推理类型，调度器首先检查设备后端、健康状态以及显存、算力和实例数量等硬约束，再结合对应的模型适配评分、共享状态与资源装箱因素对候选设备排序。分类结果作为任务与设备匹配的评分依据，资源合法性始终由原生调度流程保证。'),
            ('body','已将模型封装为常驻Python推理服务，并在Volcano中实现RF设备选择策略。策略接入节点过滤、节点评分、物理GPU选择、配额分配和释放流程，能够将模型推荐落实为具体设备UUID。系统采用请求超时、返回值校验、分配前资源复核与异常回退机制，模型服务只负责预测，资源账本继续由Volcano统一维护。'),
            ('sub','5. 调度仿真与RF独立贡献验证'),
            ('body','已建立包含4张RTX 4090、4张RTX 5090和3张L40的离散事件仿真，对比Binpack、静态分类与动态分类三种策略。实验覆盖四种负载场景，每个场景使用3个随机种子，每条任务轨迹包含120个Pod。在训练任务为主的合成仿真场景中，动态策略较Binpack平均等待时间下降11.59%，体现了动态资源适配在该负载条件下的调度价值。'),
            ('body','为单独检验模型预测的作用，进一步开展180组Volcano原生调度流程的配对离线测试。保持任务、资源快照、硬约束和启发式评分相同，仅切换RF学习项，最终有37组（20.56%）GPU UUID发生变化，其中同节点变化5组、跨节点变化32组。该结果说明，RF预测能够独立参与最终设备选择，策略效果并非仅来自原有资源过滤或启发式排序。'),
            ('sub','6. 真实集群分配与同卡动态分类验证'),
            ('body','在现有Kubernetes与Volcano集群中，已完成基础分配场景的6个sleep Pod验证，设备分配与释放流程运行正常，并验证模型服务异常时的回退。随后在四张RTX 4090上进行连续动态实验，通过对其他设备预留逻辑算力，将实验Pod限定在同一目标GPU上，观察资源变化后的重新分类。'),
            ('body','目标GPU初始逻辑显存为24564 MiB、算力配额为100。先分配1000 MiB/60单位算力的计算密集型配额，再分配17564 MiB/5单位算力的显存密集型配额，随后按顺序释放。使用轻量观测Pod触发各状态的模型调用，同一GPU的实际分类结果如下。'),
        ]
        rebuild(cell,before,patterns)
        cell.append(dynamic_table)
        after=[
            ('body','实验观察到“训推一体→训练优先→参考负载不足→推理优先→训推一体”的完整变化，全部释放后资源与类别恢复初始状态。动态场景的8次分配中，RF推荐UUID、Volcano分配UUID与Device Plugin消费UUID全部一致，逻辑配额增量核对正确，观察到超分配0次。'),
            ('body','配对离线测试用于检验RF学习项的独立作用，同卡连续实验用于验证动态分类、设备分配和资源释放的一致性。实验结束后已清理测试资源并恢复原调度器；GPU计算进程与运行环境保持原状。'),
            ('head','（二）重难点分析及拟采取的解决措施'),
            ('sub','1. 跨型号特征统一与模型适用范围'),
            ('body','不同型号的算力规格存在口径差异，缺失参数和新架构也会影响泛化判断。目前已建立逐参数来源记录、型号分组测试和验证覆盖检查。后续将结合各型号错误样本完善适用范围分析，使分类结果与其依据保持对应。'),
            ('sub','2. 动态状态与分配结果的一致性'),
            ('body','候选评分与实际分配之间可能发生资源状态变化，跨语言调用还需兼顾异常处理与调度开销。现已采用分配前复核及原生账本管理，并关联模型推荐、设备注解和插件消费记录。下一步将补充长序列与并发场景回归，提高持续运行的稳定性。'),
            ('sub','3. 类型感知调度与公平性目标协同'),
            ('body','在完成任务与设备适配的基础上，下一阶段将重点补充队列级公平性研究：加入FIFO、HRRN对照及等待时间补偿，统一平均等待、减速比、饥饿率和Jain公平性指数的评价口径，分析资源匹配效率与长期等待之间的权衡，进一步完善开题确定的公平调度目标。'),
        ]
        for role,s in after:cell.append(put(deepcopy(patterns[role]),s))

        outcell=tables[2].find('w:tr/w:tc',NS)
        outpattern=outcell.find('w:p',NS)
        outitems=[
            ('body','已形成以下与学位申请成果研究内容相关的阶段成果：'),
            ('body','1. GPU硬件画像数据库及合成资源状态数据集，配套参数来源、训练验证测试划分和分类评价记录。'),
            ('body','2. 随机森林动态适配分类模型与Pod–GPU设备选择模块，支持按任务类型评分、资源分配释放后的重新预测及异常回退。'),
            ('body','3. Volcano调度原型、模型推理服务与分配流程测试，完成三策略仿真、180组配对消融及真实Kubernetes逻辑分配验证。'),
            ('body','4. 同卡动态分类实验、设备UUID一致性与资源释放证据，以及相应数据表、图表、系统流程图和实验分析材料。'),
        ]
        rebuild(outcell,outitems,{'body':outpattern})
        nextcell=tables[3].find('w:tr/w:tc',NS)
        nextpattern=nextcell.find('w:p',NS)
        nextitems=[
            ('body','2026年10月下旬—11月上旬：整理现有分类、仿真和真实集群实验，形成稳定的系统演示及论文实验章节，统一数据来源、指标定义与图表表达。'),
            ('body','2026年11月中旬：开展公平性专项研究，补充FIFO、HRRN基线及等待补偿机制，比较不同负载下的等待时间、减速比、饥饿率与公平性。'),
            ('body','2026年11月下旬—12月中旬：完成方法与系统实现章节，补充必要的对照和稳定性测试，形成较完整的论文初稿。'),
            ('body','2026年12月下旬—2027年1月：根据导师意见完善分析与讨论，整理复现程序、系统演示和相关成果材料。'),
            ('body','2027年2月—3月：按学院安排完成论文预审、格式审核与修改，准备终稿和答辩材料。'),
        ]
        rebuild(nextcell,nextitems,{'body':nextpattern})
        final_xml=etree.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)
        with ZipFile(OUTPUT,'w') as out:
            for item in z.infolist():out.writestr(item,final_xml if item.filename=='word/document.xml' else z.read(item))
    assert SOURCE.read_bytes()==raw
    with ZipFile(SOURCE) as a,ZipFile(OUTPUT) as b:
        changed=[s for s in a.namelist() if a.read(s)!=b.read(s)]
        assert changed==['word/document.xml']
        content=b.read('word/document.xml').decode('utf-8')
        assert content.count(TITLE)==3
        for s in ['DQN','fake-client','第七轮','第八轮','rf-job','rf-v4-known-states','1.86%']:
            assert s not in content,s
    record={'source':str(SOURCE),'source_sha256':sha(raw),'output':str(OUTPUT),'output_sha256':sha(OUTPUT.read_bytes()),'changed_parts':changed,'title_preserved':True,'progress_characters':sum(len(s) for _,s in before+after),'experiment_data_and_models_unchanged':True,'audience':'中期考核评审','editorial_scope':'研究问题、方法、代表性成果、下一步工作；保留实验性质与结果的准确范围'}
    (HERE/'authoring-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'artifact.md').write_text('Retained reference: '+str(SOURCE)+'; SHA256 '+sha(raw)+'. Reuse the original midterm template and the 20261023 formatting contract. Preserve all four sections, cover, identity and date, all table geometry, nested dynamic-state table, review and signature fields. Intentionally rewrite abstract, progress, outcomes and next steps for academic reviewers. Only word/document.xml changes; all other ZIP parts remain byte-identical. Summarize evidence without development round names, implementation aliases, identifiers or debugging logs. Keep synthetic classification and simulation distinguished from real Kubernetes logical allocation. Fairness remains planned work. No model, labels, score weights, experiment data or source code changes. Native KWPS PDF fallback and bundled pypdfium2 are used for visual QA because packaged LibreOffice is unavailable. Inspect every final page and preserve the earlier reports.\n',encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))

if __name__=='__main__':main()

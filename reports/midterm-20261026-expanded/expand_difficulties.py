from pathlib import Path
from zipfile import ZipFile
from copy import deepcopy
from lxml import etree
import json,hashlib,io,os,shutil

HERE=Path(__file__).resolve().parent
SOURCE=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_定稿.docx')
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W='{'+NS['w']+'}'
HEADING='（二）重难点分析及拟采取的解决措施'
TITLE='异构vGPU集群的性能感知与公平调度设计与实现'
sha=lambda b:hashlib.sha256(b).hexdigest()

def ptext(p):return ''.join(t.text or '' for t in p.findall('.//w:t',NS))

def main():
    before=SOURCE.read_bytes()
    backup=HERE/'中期报告_扩写前.docx'
    if not backup.exists():shutil.copy2(SOURCE,backup)
    assert backup.read_bytes()==before
    items=[
        ('head','1. 异构GPU硬件特征的统一描述'),
        ('body','不同型号GPU的公开规格在计量单位、计算精度和算力口径上存在差异，部分参数还存在缺失，直接拼接容易造成特征含义不一致。例如，稀疏算力与非稀疏算力、普通FP32算力与Tensor计算能力需要分别记录，同一系列的不同板卡形态也应单独区分。针对这些问题，已建立逐参数来源记录，统一容量、带宽和算力字段，保留原始单位及规格说明，并完成可比输入特征的筛选。'),
        ('body','对于未公开的参数，数据库保留缺失标记，由模型预处理流程统一处理；缺失值处理参数仅由训练集拟合，保证不同数据划分之间的独立性。在此基础上，将静态硬件画像与剩余显存、算力配额、实例数量等动态状态连接，形成统一的模型输入接口。目前已完成公开参数库、特征处理和分类调用流程，并用于后续仿真及Volcano集成，解决了不同设备信息难以统一组织和直接用于调度判断的问题。'),
        ('head','2. 模型评分与最终GPU设备分配的衔接'),
        ('body','随机森林模型由Python实现，Volcano调度器由Go实现，模型返回候选设备排名后，还需要经过节点筛选、物理GPU选择和原生分配。这里的关键问题是让模型评分真正作用于最终设备UUID，同时保证显存、算力和实例数量约束始终有效。为此，已将模型封装为常驻推理服务，建立Go与Python之间的通信接口，并将RF排序接入Volcano原生设备选择与分配流程。'),
        ('body','在执行过程中，调度器根据当前逻辑资源构造候选快照，对返回UUID进行校验，并在分配前复核设备状态；资源扣减和释放继续由原生账本维护。针对服务不可用等情况，已实现确定性回退并完成验证。在180组配对离线测试中，保持其他评分条件一致，仅切换RF学习项，有37组最终GPU UUID发生变化。真实集群实验进一步核对了模型推荐、Volcano分配和设备插件接收的UUID，完成了从模型评分到实际设备分配的衔接。'),
        ('head','3. 同一GPU动态类别变化的验证'),
        ('body','初期真实Pod实验已验证资源分配与释放，但多次分类结果均为训推一体，难以直观展示剩余资源变化带来的适配性调整。针对这一问题，已先分析同一GPU在不同剩余配额下的模型输出，再设计计算密集型与显存密集型资源申请组合。在保持模型和分类标准不变的条件下，使连续实验Pod合法分配到同一张RTX 4090，按顺序执行资源分配、状态读取、重新分类和资源释放。'),
        ('body','具体实验先申请1000 MiB显存及60单位算力，再申请17564 MiB显存及5单位算力，随后依次释放。实际观察到目标GPU完成“训推一体→训练优先→参考负载不足→推理优先→训推一体”的完整变化。整个动态实验场景的8次分配均完成UUID一致性核对，显存、算力和实例配额更新正确；全部释放后，目标GPU的资源与分类恢复初始状态。由此完成了同一设备动态分类过程及分配释放联动的实际验证。'),
        ('head','4. 公平性调度的进一步完善'),
        ('body','在完成动态适配分类和设备级分配的基础上，后续将重点研究任务匹配效率与排队公平性的协调。计划复用现有仿真器，补充FIFO、HRRN及等待时间补偿对照，在相同任务序列、资源约束和随机种子下比较不同策略。评价指标包括平均等待时间、任务减速比、饥饿率和Jain公平性指数，并结合训练与推理任务比例分析变化趋势，逐步完善开题确定的公平调度目标。'),
    ]
    with ZipFile(io.BytesIO(before)) as z:
        root=etree.fromstring(z.read('word/document.xml'))
        body=root.find('w:body',NS)
        cell=body.findall('w:tbl',NS)[1].find('w:tr/w:tc',NS)
        head=next(p for p in cell.findall('w:p',NS) if ptext(p)==HEADING)
        after=list(cell)[list(cell).index(head)+1:]
        assert len(after)==8,'Preserve unexpected edits.'
        patterns={'head':deepcopy(after[0]),'body':deepcopy(after[1])}
        for p in after:cell.remove(p)
        for role,content in items:
            p=deepcopy(patterns[role]);ts=p.findall('.//w:t',NS)
            assert len(ts)==1
            ts[0].text=content
            cell.append(p)
        # Keep the original outcomes/next-steps/signature page pattern intact.
        outcome_heading=next(p for p in body.findall('w:p',NS) if ptext(p)=='三、已取得的与学位申请成果研究内容相关的成果')
        pp=outcome_heading.find('w:pPr',NS)
        if pp is None:pp=etree.SubElement(outcome_heading,W+'pPr')
        pagebreak=pp.find('w:pageBreakBefore',NS)
        if pagebreak is None:pagebreak=etree.SubElement(pp,W+'pageBreakBefore')
        pagebreak.set(W+'val','true')
        xml=etree.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)
        buffer=io.BytesIO()
        with ZipFile(buffer,'w') as out:
            for i in z.infolist():out.writestr(i,xml if i.filename=='word/document.xml' else z.read(i))
    payload=buffer.getvalue();tmp=HERE/'expanded.tmp';tmp.write_bytes(payload)
    assert SOURCE.read_bytes()==before
    output=SOURCE;locked=False
    try:os.replace(tmp,output)
    except PermissionError:
        output=SOURCE.with_name('中期报告_张兵昇_定稿_扩写版.docx')
        assert not output.exists(),'Preserve existing file.'
        os.replace(tmp,output);locked=True
    with ZipFile(io.BytesIO(before)) as a,ZipFile(output) as b:
        changed=[n for n in a.namelist() if a.read(n)!=b.read(n)]
        assert changed==['word/document.xml']
        assert b.read('word/document.xml').decode('utf-8').count(TITLE)==3
    record={'source':str(SOURCE),'output':str(output),'backup':str(backup),'source_sha256':sha(before),'output_sha256':sha(payload),'original_file_locked':locked,'changed_parts':changed,'section_characters':sum(len(s) for _,s in items),'new_section':items,'layout_change':'第三部分另起一页，保留成果、后续安排及签名栏同页的模板布局','title_model_data_and_results_unchanged':True}
    (HERE/'revision-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'artifact.md').write_text('Retained reference: '+str(backup)+'; SHA256 '+sha(before)+'. Reuse existing midterm template, typography and four sections. Expand only the difficulty subsection into problem, completed method and observed results for three issues, plus one future fairness item. Preserve every exact title, all metadata and signatures, source data and dynamic-state table. Body prose remains 14pt with original exact21pt line spacing. Page flow may expand naturally; start the existing third part on a fresh page to keep outcomes, next steps and signatures together. No font shrinking. Every package part except document.xml stays byte-identical. Use native KWPS export and bundled pypdfium2 for the established Windows rendering fallback. Inspect all changed pages; compare remaining pages with previously inspected renders. No model, experiment or cluster modifications.\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k!='new_section'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

from pathlib import Path
from zipfile import ZipFile
from lxml import etree
import hashlib
import json
import shutil

HERE=Path(__file__).resolve().parent
SOURCE=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_已填写.docx')
OUTPUT=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_修订版.docx')
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W='{'+NS['w']+'}'
TITLE='异构vGPU集群的性能感知与公平调度设计与实现'

def sha(data): return hashlib.sha256(data).hexdigest()

def main():
    original=SOURCE.read_bytes()
    retained=HERE/'中期报告_本次修订前.docx'
    if not retained.exists(): shutil.copy2(SOURCE,retained)
    assert sha(retained.read_bytes())==sha(original)
    changes=[]
    with ZipFile(SOURCE) as z:
        root=etree.fromstring(z.read('word/document.xml'))
        for p in root.findall('.//w:p',NS):
            ts=p.findall('.//w:t',NS)
            text=''.join(t.text or '' for t in ts)
            new=text
            new=new.replace('以及Volcano中rf-job设备选择策略。','以及Volcano中基于随机森林的GPU设备选择策略。')
            new=new.replace('调度端采用可解释评分与Volcano原生资源账本，不引入新的DQN策略。','调度端采用可解释评分与Volcano原生资源账本。')
            if text.startswith('监督标签采用确定性理论模型：'):
                new=('为训练和推理分别定义参考负载，明确显存、算力及理论耗时要求。训练参考负载的显存需求为8192 MiB、最低算力配额为20，推理参考负载分别为4096 MiB和10。标签结合抽象工作单元的计算量、访存量及理论耗时门槛生成，具体参数保存在参考负载合同中。上述门槛用于构造合成适配标签，不是完整训练或推理任务的实测耗时。')
            if text.startswith('在合成理论标签上，第四轮'):
                new=('在合成理论标签上，第四轮已知型号新状态测试准确率为100.00%，宏平均F1（Macro-F1，即四类F1分数的等权平均）为1.0000；同架构未见RTX 6000 Ada测试准确率为96.70%，Macro-F1为0.9547。完全留出架构时仍存在覆盖不足，不能仅凭原始准确率认定未知GPU可靠。当前随机森林由120棵决策树组成，后续集成和真实实验使用同一模型，保持模型参数及分类门槛不变。')
            if text.startswith('在独立Volcano v1.13.1源码中'):
                new=('在独立Volcano v1.13.1源码中新增默认关闭的“RF设备选择策略”（实现名称为rf-job），接入节点过滤、节点评分、设备分配及资源释放流程。该策略根据Pod的任务类型与GPU当前适配评分选择具体物理设备。RF排序参与最终GPU UUID选择及注解写入，分配前重新检查当前账本。原有调度器配置及二进制保持不变，已完成Linux下Go与Python测试及分配释放回归。')
            new=new.replace('在训练为主场景，动态策略较Binpack平均等待下降11.59%；显存密集场景训练完成时间反而上升1.86%，说明当前策略并非全面占优。上述均为合成仿真指标。','在训练任务为主的合成仿真场景中，动态策略较Binpack的平均等待时间下降11.59%。该结果用于初步评价当前策略在相应负载条件下的调度效果。')
            if text.startswith('原生Go流程与真实RF HTTP服务的180组配对消融中'):
                new=('在Volcano原生调度代码与RF服务的180组配对离线测试中，保持Pod、资源快照、硬约束和启发式权重相同，仅开关RF学习项，最终GPU UUID改变37组（20.56%），其中同节点变化5组、跨节点变化32组，表明模型预测能够独立影响设备选择。测试通过模拟Kubernetes API固定资源条件，便于比较RF开关前后的决策。待验证设备采用保守回退，其排序变化不计作RF学习项贡献；真实分配与释放流程另由Kubernetes sleep Pod实验验证。')
            new=new.replace('3. 完成Volcano rf-job源码、Go–Python协议、原生分配释放测试及180组RF消融报告。','3. 完成Volcano RF设备选择策略源码、Go–Python协议、原生分配释放测试及180组RF消融报告。')
            if new!=text:
                assert len(ts)==1, 'Narrow patch expects one plain text run'
                ts[0].text=new
                changes.append({'before':text,'after':new})
        assert len(changes)==8,len(changes)
        xml=etree.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)
        with ZipFile(OUTPUT,'w') as out:
            for i in z.infolist(): out.writestr(i,xml if i.filename=='word/document.xml' else z.read(i))
    assert SOURCE.read_bytes()==original
    with ZipFile(SOURCE) as a, ZipFile(OUTPUT) as b:
        changed=[name for name in a.namelist() if a.read(name)!=b.read(name)]
        assert changed==['word/document.xml'],changed
        body=b.read('word/document.xml').decode('utf-8')
        assert body.count(TITLE)==3
        for word in ['DQN','rf-v4-known-states','1.86%','fake-client','冻结部署模型']:
            assert word not in body,word
    record={'source':str(SOURCE),'source_sha256':sha(original),'output':str(OUTPUT),'output_sha256':sha(OUTPUT.read_bytes()),'changed_parts':changed,'changes':changes,'title_preserved':True,'model_and_experiment_artifacts_unchanged':True}
    (HERE/'revision-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'artifact.md').write_text('Narrow prose edit of the retained filled midterm report. Source: '+str(SOURCE)+'; SHA256 '+sha(original)+'. All format, sections, tables, personal metadata, signatures, review boxes and package parts are preserved; only eight plain text paragraphs in word/document.xml change. Reuse the 20261023 template contract and page design. Cover/report date remains the existing date; no experiment data, model parameters, rules or scores change. Remove irrelevant historical DQN details and model identifier; explain Macro-F1 and RF strategy; replace abstract-time and fake-client jargon with accessible descriptions. Summarize the supported training-heavy result without claiming universal improvement. Full experimental results remain unchanged in project evidence. Render with the native KWPS fallback documented in the previous task because packaged LibreOffice is unavailable on this host, and inspect every page.\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k!='changes'},ensure_ascii=False,indent=2))

if __name__=='__main__': main()

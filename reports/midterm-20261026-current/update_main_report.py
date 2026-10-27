from pathlib import Path
from zipfile import ZipFile
from lxml import etree
import hashlib,json,shutil,io,os

HERE=Path(__file__).resolve().parent
SOURCE=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_汇报版.docx')
OUTPUT=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_修订版.docx')
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
TITLE='异构vGPU集群的性能感知与公平调度设计与实现'
sha=lambda b:hashlib.sha256(b).hexdigest()

def main():
    output=OUTPUT
    before_target=OUTPUT.read_bytes()
    before_source=SOURCE.read_bytes()
    assert sha(before_target)=='2943a1cef326f8fe012ae2885ce2421e02472a8227e44c27e60b72f55bc92edd','Target changed since previous authoring; preserve user edits.'
    for p in [SOURCE,OUTPUT]:
        backup=HERE/(p.stem+'_同步前.docx')
        if not backup.exists():shutil.copy2(p,backup)
    changes=[]
    with ZipFile(SOURCE) as z:
        x=etree.fromstring(z.read('word/document.xml'))
        for p in x.findall('.//w:p',NS):
            ts=p.findall('.//w:t',NS)
            old=''.join(t.text or '' for t in ts)
            new=None
            if old.startswith('实验观察到“训推一体→训练优先'):
                new=('真实集群实验观察到同一GPU完成“训推一体→训练优先→参考负载不足→推理优先→训推一体”的完整变化。动态场景的8次分配中，RF推荐、Volcano实际分配与设备插件接收的GPU UUID全部一致，逻辑显存、算力和实例配额更新正确，未观察到超分配；全部释放后资源与分类恢复初始状态。')
            elif old.startswith('配对离线测试用于检验RF学习项的独立作用'):
                new=('后续Pod到达时，系统能够读取更新后的剩余配额，重新执行适配分类和设备选择。上述结果验证了任务提交、动态分类、设备分配与资源释放的完整调度流程，为进一步研究异构资源匹配和公平调度提供了可运行的实验基础。')
            if new is not None:
                assert len(ts)==1
                ts[0].text=new
                changes.append({'before':old,'after':new})
        assert len(changes)==2
        xml=etree.tostring(x,encoding='UTF-8',xml_declaration=True,standalone=True)
        buf=io.BytesIO()
        with ZipFile(buf,'w') as out:
            for i in z.infolist():out.writestr(i,xml if i.filename=='word/document.xml' else z.read(i))
    payload=buf.getvalue()
    # Do not overwrite changes made by the user during this narrow edit.
    assert OUTPUT.read_bytes()==before_target
    temporary=HERE/'updated-report.tmp'
    temporary.write_bytes(payload)
    locked_target=False
    try:
        os.replace(temporary,output)
    except PermissionError:
        # The user is viewing the old revision. Never close their application.
        output=OUTPUT.with_name('中期报告_张兵昇_定稿.docx')
        assert not output.exists(),'Preserve an existing final report.'
        os.replace(temporary,output)
        locked_target=True
    assert SOURCE.read_bytes()==before_source
    with ZipFile(SOURCE) as a,ZipFile(output) as b:
        changed=[s for s in a.namelist() if a.read(s)!=b.read(s)]
        assert changed==['word/document.xml']
        body=b.read('word/document.xml').decode('utf-8')
        assert body.count(TITLE)==3
        for s in ['插件UUID依据Pod UID','完全留出架构时仍存在覆盖不足','由于同卡实验只有一个合法候选','DQN','fake-client','第七轮','第八轮','rf-job']:
            assert s not in body,s
    record={'source':str(SOURCE),'output':str(output),'old_target_sha256':sha(before_target),'source_sha256':sha(before_source),'output_sha256':sha(payload),'changed_from_audience_report':changed,'changes':changes,'previous_revision_backed_up':True,'title_and_experiment_results_unchanged':True,'model_and_experiment_artifacts_unchanged':True,'old_revision_locked_by_viewer':locked_target}
    (HERE/'change-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'artifact.md').write_text('Continue editing the existing desktop 修订版 as the main report. Retain both preceding reports in this task directory. Use the already visually reviewed 8-page 汇报版 as the current content baseline, and replace only its two real-experiment concluding paragraphs with direct, factual outcomes. Preserve original title, all geometry, identity/date, dynamic state table, review forms and signatures. Only word/document.xml changes from the 8-page baseline. Keep the existing concise experimental setup identifying synthetic labels/simulation and real sleep Pod logical-allocation validation. No evidence files, model, classifier thresholds or results are changed. Render native KWPS PDF with bundled pypdfium2 and inspect changed pages; verify other pages against previously inspected render.\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k!='changes'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

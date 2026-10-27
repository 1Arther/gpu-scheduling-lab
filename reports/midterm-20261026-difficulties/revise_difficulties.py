from pathlib import Path
from zipfile import ZipFile
from copy import deepcopy
from lxml import etree
import json,hashlib,io,os,shutil

HERE=Path(__file__).resolve().parent
SOURCE=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_定稿.docx')
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
TITLE='异构vGPU集群的性能感知与公平调度设计与实现'
HEADING='（二）重难点分析及拟采取的解决措施'
sha=lambda x:hashlib.sha256(x).hexdigest()

def ptext(p):return ''.join(t.text or '' for t in p.findall('.//w:t',NS))

def main():
    live_before=SOURCE.read_bytes()
    backup=HERE/'中期报告_张兵昇_定稿_重难点修订前.docx'
    if not backup.exists():shutil.copy2(SOURCE,backup)
    previous=HERE/'revision-record.json'
    if previous.exists():
        old_record=json.loads(previous.read_text(encoding='utf-8'))
        assert sha(live_before)==old_record['after_sha256'],'Preserve user edits between rendering iterations.'
    before=backup.read_bytes()
    items=[
        ('head','1. 异构GPU硬件特征的统一描述'),
        ('body','针对不同型号规格口径不一致和参数缺失问题，已统一单位、区分稀疏与非稀疏算力，完成可比特征筛选与缺失值处理，形成统一的硬件画像及模型输入接口。'),
        ('head','2. 模型评分与最终GPU设备分配的衔接'),
        ('body','针对模型评分落实到物理设备选择的问题，已将RF排序接入Volcano原生分配并执行资源复核。180组配对离线测试中37组最终UUID因学习项而改变，真实集群完成了推荐、分配与插件接收UUID的一致性验证。'),
        ('head','3. 同一GPU动态类别变化的验证'),
        ('body','初期真实实验中分类始终为训推一体。通过同卡连续分配、释放两种资源配额，已在RTX 4090上观察到四类完整切换；8次分配UUID一致、配额更新正确，释放后资源及分类恢复初始状态。'),
        ('head','4. 公平性调度的进一步完善'),
        ('body','后续补充FIFO、HRRN及等待时间补偿对照，以等待时间、饥饿率和Jain指数完善公平性评价。'),
    ]
    with ZipFile(io.BytesIO(before)) as z:
        root=etree.fromstring(z.read('word/document.xml'))
        table=root.find('w:body',NS).findall('w:tbl',NS)[1]
        cell=table.find('w:tr/w:tc',NS)
        paragraphs=cell.findall('w:p',NS)
        heading=next(p for p in paragraphs if ptext(p)==HEADING)
        idx=list(cell).index(heading)
        following=list(cell)[idx+1:]
        assert len(following)==6,'Preserve any unexpected user additions.'
        patterns={'head':deepcopy(following[0]),'body':deepcopy(following[1])}
        old_text=[ptext(p) for p in following]
        for el in following:cell.remove(el)
        for role,content in items:
            p=deepcopy(patterns[role])
            ts=p.findall('.//w:t',NS)
            assert len(ts)==1
            ts[0].text=content
            cell.append(p)
        xml=etree.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)
        buf=io.BytesIO()
        with ZipFile(buf,'w') as out:
            for i in z.infolist():out.writestr(i,xml if i.filename=='word/document.xml' else z.read(i))
    payload=buf.getvalue()
    tmp=HERE/'updated.tmp'
    tmp.write_bytes(payload)
    assert SOURCE.read_bytes()==live_before
    output=SOURCE
    locked=False
    try:os.replace(tmp,output)
    except PermissionError:
        output=SOURCE.with_name('中期报告_张兵昇_定稿_重难点修订.docx')
        assert not output.exists(),'Preserve existing user file.'
        os.replace(tmp,output)
        locked=True
    with ZipFile(io.BytesIO(before)) as a,ZipFile(output) as b:
        changed=[n for n in a.namelist() if a.read(n)!=b.read(n)]
        assert changed==['word/document.xml']
        assert b.read('word/document.xml').decode('utf-8').count(TITLE)==3
    receipt={'source':str(SOURCE),'output':str(output),'backup':str(backup),'before_sha256':sha(before),'after_sha256':sha(payload),'old_section':old_text,'new_section':items,'changed_package_parts':changed,'original_open_and_locked':locked,'scope':'只替换重难点小节：3项已解决问题、措施与结果；1项继续完善','body_characters':sum(len(s) for _,s in items)}
    (HERE/'revision-record.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'artifact.md').write_text('Reference: retained backup '+str(backup)+', SHA256 '+sha(before)+'. Keep all preceding 8-page midterm-report content and template appearance. Edit only the final difficulty subsection in the progress cell; retain its exact section heading. Replace the previous three broad difficulty items with three documented resolved issues (hardware-feature normalization, RF influence on final device UUID, controlled same-GPU dynamic categories) and one concise future fairness item. All facts derive from existing reports and evidence, without new experiments. Preserve title, identity/date, section geometry, dynamic-state table, outcome and future-plan tables, review fields, signatures and all package parts except document.xml. Use native KWPS PDF fallback already documented and bundled pypdfium2 for page review. Original copied before edit; no model, data or scoring changes.\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k not in ['old_section','new_section']},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

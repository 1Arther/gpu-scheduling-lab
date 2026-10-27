from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from lxml import etree

QA = Path(__file__).resolve().parent
BASE = QA.parents[1]
REFERENCE = Path('C:/Users/EDY/Desktop/中期报告.docx')
PROPOSAL = Path('C:/Users/EDY/Desktop/开题最终.doc')
OUTPUT = Path('C:/Users/EDY/Desktop/中期报告_张兵昇_已填写.docx')
TITLE = '异构vGPU集群的性能感知与公平调度设计与实现'
EN_TITLE = ('Design and Implementation of Performance-Aware and Fairness-Oriented '
            'Scheduling for Heterogeneous vGPU Clusters')


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def package_inventory(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: {'size': i.file_size, 'sha256': hashlib.sha256(z.read(i)).hexdigest()}
                for i in z.infolist()}


def font(run, size=14, bold=False, underline=False):
    run.font.name = '宋体'
    run.font.size = Pt(size)
    run.bold = bold
    run.underline = underline
    rp = run._r.get_or_add_rPr()
    rf = rp.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts')
        rp.insert(0, rf)
    for k in ['asciiTheme', 'hAnsiTheme', 'eastAsiaTheme', 'cstheme']:
        rf.attrib.pop(qn('w:' + k), None)
    for k, v in [('ascii', 'Times New Roman'), ('hAnsi', 'Times New Roman'), ('eastAsia', '宋体')]:
        rf.set(qn('w:' + k), v)


def paragraph(p, text, size=14, bold=False, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
              indent=True, line=1.5, keep=False, before=0, after=0):
    p.clear()
    p.alignment = align
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = Pt(size * line)
    pf.first_line_indent = Pt(size * 2 if indent else 0)
    pf.left_indent = Pt(0)
    pf.right_indent = Pt(0)
    pf.keep_with_next = keep
    pf.keep_together = False
    pf.widow_control = True
    pp = p._p.get_or_add_pPr()
    sp = pp.find(qn('w:spacing'))
    if sp is not None:
        for k in ['beforeLines','afterLines']:
            sp.attrib.pop(qn('w:' + k),None)
    marker = pp.find(qn('w:rPr'))
    if marker is not None:
        for el in list(marker):
            if el.tag in [qn('w:sz'), qn('w:szCs')]:
                el.set(qn('w:val'),str(int(size * 2)))
    for el in list(pp):
        if el.tag in [qn('w:numPr'), qn('w:tabs')]:
            pp.remove(el)
    ind = pp.find(qn('w:ind'))
    if ind is not None:
        for k in ['firstLineChars', 'hangingChars', 'leftChars', 'rightChars', 'hanging']:
            ind.attrib.pop(qn('w:' + k), None)
    snap = pp.find(qn('w:snapToGrid'))
    if snap is None:
        snap = OxmlElement('w:snapToGrid')
        pp.append(snap)
    snap.set(qn('w:val'), '0')
    r = p.add_run(text)
    font(r, size, bold)
    return p


def fill_cell(cell, text, size=12, center=False, line=1.25):
    # Only editable text content is replaced; cell geometry/merges are retained.
    for child in list(cell._tc):
        if child.tag != qn('w:tcPr'):
            cell._tc.remove(child)
    p = cell.add_paragraph()
    paragraph(p, text, size=size,
              align=WD_ALIGN_PARAGRAPH.CENTER if center else WD_ALIGN_PARAGRAPH.JUSTIFY,
              indent=False, line=line)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def fill_block(cell, blocks, size=14):
    for child in list(cell._tc):
        if child.tag != qn('w:tcPr'):
            cell._tc.remove(child)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    for kind, text in blocks:
        p = cell.add_paragraph()
        paragraph(p, text, size=size, bold=kind in ('head', 'sub'),
                  indent=kind == 'body', keep=kind in ('head', 'sub'),
                  before=5 if kind == 'head' else 2 if kind == 'sub' else 0,
                  after=3 if kind in ('head', 'sub') else 2)


def cover_field(p, label, value):
    p.clear()
    r = p.add_run(label + ':')
    font(r, 22)
    r = p.add_run('  ' + value + '  ')
    font(r, 22, underline=True)


def dynamic_table(cell):
    t = cell.add_table(rows=1, cols=5)
    t.autofit = False
    widths = [0.55, 1.25, 1.15, 0.8, 2.62]
    for c, w in zip(t.columns, widths):
        c.width = Inches(w)
    pr = t._tbl.tblPr
    b = OxmlElement('w:tblBorders')
    for edge in ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']:
        e = OxmlElement('w:' + edge)
        e.set(qn('w:val'), 'single')
        e.set(qn('w:sz'), '4')
        e.set(qn('w:color'), '000000')
        b.append(e)
    pr.append(b)
    hdr = ['状态', '剩余显存/MiB', '剩余算力配额', '实例数', 'RF实际类别']
    for c, s in zip(t.rows[0].cells, hdr):
        fill_cell(c, s, 11, True, 1.15)
        for r in c.paragraphs[0].runs:
            r.bold = True
    repeat = OxmlElement('w:tblHeader')
    t.rows[0]._tr.get_or_add_trPr().append(repeat)
    data = [
        ['S0', '24564', '100', '0', '训推一体'],
        ['S1', '23564', '40', '1', '训练优先'],
        ['S2', '6000', '35', '2', '参考负载不足'],
        ['S3', '7000', '95', '1', '推理优先'],
        ['S4', '24564', '100', '0', '训推一体'],
    ]
    for row in data:
        rr = t.add_row()
        for c, s in zip(rr.cells, row):
            fill_cell(c, s, 11, True, 1.15)
        cant = OxmlElement('w:cantSplit')
        rr._tr.get_or_add_trPr().append(cant)
    return t


def main():
    baseline = package_inventory(REFERENCE)
    (QA / 'template-package-inventory.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding='utf-8')
    retained = QA / '中期报告_原始模板.docx'
    if not retained.exists():
        shutil.copy2(REFERENCE, retained)
    assert sha(retained) == sha(REFERENCE)
    (QA / 'artifact.md').write_text(f'''# 中期报告模板执行合同

Reference: {REFERENCE}; retained copy: {retained}
SHA256: {sha(REFERENCE)}. Original proposal SHA256: {sha(PROPOSAL)}.
Reference render: template-reference.pdf and template-1.png through template-6.png.
Native bundled LibreOffice unavailable on Windows (renderer logged missing soffice.exe).
QA fallback uses installed KWPS COM read-only SaveAs PDF, then bundled pypdfium2 renders every page.

Four unchanged portrait A4 sections: 11906×16838 twips; all margins1191twips;
header851twips; cover footer992twips, subsequent footer567twips.
Section2 restarts page numbers at1, footer1/footer2 PAGE fields and relationships preserved.
Named/actual paragraph roles: cover school26pt华文行楷, title26pt黑体,
cover fields22pt宋体 with underlined answers; long thesis title uses18pt on the existing second line.
Body headings15–16pt黑体; progress original14pt宋体 1.5line; short cells12pt宋体.
The blank instructional page and all explanations remain unchanged. Cover 样表 placeholder is cleared.

Slot map, stable locators in word/document.xml:
body/p[5:15] cover title/personnel/date (python-docx 0-based4:14); title unchanged verbatim.
tbl[1] rows0,1,2,4,5,6,8,9,10: identity, enrollment/application type, Chinese/English
title, abstract, keywords, source. Values copied from proposal, not invented.
tbl[2]/tr[1]/tc[1]: replace prompts with progress and problems; retain frame/grid;
allow this editable long row to split across pages, removing its blank-form minimum height.
This is an authorized content expansion of the original progress slot, expected to add body pages.
A small black ruled nested table uses the source table style and displays actual five-state observations.
tbl[3]/tr[1]/tc[1]: other research outputs, no fabricated publication or patent.
tbl[4]/tr[1]/tc[1]: next work and dated planned schedule.
tbl[5] rows0:5: basic assessment metadata and exact thesis title.
Unedited: all advisor opinions/signatures, reviewer list, pass/fail/evaluation boxes,
student signature, actual signature dates. Optional second advisor remains blank.

Preserve-only package parts: every ZIP member except word/document.xml and word/settings.xml;
including styles, theme, numbering, fontTable, both footers, endnotes, footnotes,
all customXml, all relationships and document properties. Baseline hashes in template-package-inventory.json.
No textboxes, controls or images in the reference. Nested review pass/fail table stays untouched.
UpdateFields is set true for automatic PAGE refresh on open; PDF export verifies pagination.
Fidelity: same4sections/geometry, recognizable source black grid typography and all form sections;
no clipped text, blank overflow pages, lost signature blocks, or falsely completed review fields.
All final pages must be visually inspected. Original proposal and template are retained unchanged.
''', encoding='utf-8')

    d = Document(REFERENCE)
    p = d.paragraphs
    p[2].clear()
    p[4].clear()
    font(p[4].add_run('学位申请成果题目:'), 22)
    paragraph(p[5], TITLE, size=18, align=WD_ALIGN_PARAGRAPH.CENTER, indent=False, line=1.0)
    p[5].runs[0].underline = True
    for idx, label, value in [
        (6, '学生姓名', '张兵昇'),
        (7, '学生学号', 'SA24218208'),
        (8, '校内导师', '卢汉成'),
        (9, '第二导师', '（选填）'),
        (10, '实践导师', '杨春鲜'),
        (11, '所在院系', '先进技术研究院'),
        (12, '专业类别', '计算机技术'),
        (13, '专业领域', '算力调度'),
        (14, '填表日期', '2026年10月23日'),
    ]:
        cover_field(p[idx], label, value)
    p[19].paragraph_format.page_break_before=True
    p[19].paragraph_format.keep_with_next=True

    t = d.tables[0]
    for row, col, text in [(0,3,'张兵昇'), (0,7,'SA24218208'), (0,9,'男')]:
        fill_cell(t.cell(row,col), text, center=True)
    fill_cell(t.cell(1,3),'☑ 全日制                □ 非全日制', center=True)
    fill_cell(t.cell(2,0),'☑\n专业学位\n硕士', center=True)
    fill_cell(t.cell(2,2),'□ 专题研究类论文   □ 调研报告   □ 案例分析报告   □ 产品设计（作品创作）\n☑ 方案设计   □ 其他', line=1.5)
    fill_cell(t.cell(4,5),TITLE,12,True)
    fill_cell(t.cell(5,5),EN_TITLE,11,False,1.05)
    abstract = (
        '本研究面向异构vGPU集群，围绕性能特征感知、任务与资源适配及公平调度展开。'
        '目前已建立公开GPU硬件参数库，将静态能力与剩余逻辑显存、算力配额和任务组成联合建模，'
        '采用随机森林预测参考训练、推理负载的适配性，并提供信息不足时的保守回退。'
        '已实现Pod类型解析、GPU候选过滤与排序，以及Volcano中rf-job设备选择策略。'
        '通过合成数据分类、三策略调度仿真和原生Go消融检验模型作用；在真实Kubernetes中完成sleep Pod分配、'
        'UUID传递及资源释放，并观察到同一RTX 4090随配额变化完成四类切换。'
        '当前结果验证了逻辑调度原型的可运行性；分类标签来自合成参考合同，尚不代表真实性能，'
        '公平性专项评价仍需补充。'
    )
    fill_cell(t.cell(6,2),abstract,12,False,1.2)
    fill_cell(t.cell(8,4),'异构vGPU 动态分类 随机森林 资源调度 公平性',12)
    fill_cell(t.cell(9,4),'Heterogeneous vGPU / Dynamic Classification / Random Forest / Resource Scheduling / Fairness',11,False,1.1)
    fill_cell(t.cell(10,1),'无',12)

    progress = d.tables[1].cell(0,0)
    for el in list(d.tables[1].rows[0]._tr.get_or_add_trPr()):
        if el.tag in [qn('w:cantSplit'),qn('w:trHeight')]:
            el.getparent().remove(el)

    first = [
        ('head','（一）总体进展情况'),
        ('sub','1. 研究目标与技术路线落实'),
        ('body','本课题围绕“异构vGPU性能感知”和“类型感知与公平调度”两项内容推进。目前已形成从硬件画像、动态资源建模、适配预测，到设备选择、配额分配及释放的可运行原型。用户通过Pod标签声明training或inference，系统判断当前GPU对参考负载的适配性，不自动识别容器内程序。'),
        ('body','在实现过程中，将开题提出的静态无监督聚类路线调整为基于明确参考负载合同的RandomForest双目标预测，避免仅凭产品定位给GPU贴标签。调度端采用可解释评分与Volcano原生资源账本，不引入新的DQN策略。开题规划的GNN/PPO与HRRN公平性增强尚未完成，后续将单独评估，不以现有资源均衡项替代公平性结论。'),
        ('sub','2. 硬件画像、动态特征与分类模型'),
        ('body','已整理7个厂商22款设备的公开参数及来源，并针对NVIDIA进一步核实15种硬件配置。数据保存容量、带宽、FP32、FP16、BF16、INT8和最大板卡功耗等字段，区分稀疏与非稀疏、普通计算与Tensor口径以及不同板卡形态。未公开数据保留缺失，不将型号、厂商或架构字符串作为必须依赖的数值分类特征；NPU仅保留接口映射。'),
        ('body','动态特征包括剩余逻辑显存、剩余算力配额、已分配实例数、已有训练及推理实例数，并增加资源比例和有效FP32代理。以训练、推理两个适配判断组合出训练优先、推理优先、训推一体、参考负载不足四类；缺少关键输入或相关验证覆盖时返回待验证。物理GPU能力保持不变，变化的是剩余逻辑资源的适配性。'),
        ('body','监督标签采用确定性理论模型：FP32训练参考负载需8192 MiB、至少20单位配额，理论完成时间不超过0.2 s；推理参考负载需4096 MiB、至少10单位配额，时间不超过0.01 s。工作量分别为2.0/0.06 TFLOP，访存量为8.0/2.5 GB；算力、带宽效率假设分别为0.35和0.55。以上是合成参考合同，不是实测模型性能，也不是物理GPU固有类型。'),
        ('sub','3. 数据划分、泛化评价与模型冻结'),
        ('body','已保留训练、验证、测试划分及样本审计。第四轮共生成7710个合成资源状态；默认已知型号协议使用3667/1240/1244个训练、验证、测试样本，另按型号完全留出进行泛化测试。插补与模型仅在训练集拟合，验证集选择特征和参数；跨型号测试不混入同型号状态，未将预测结果回灌为监督标签。'),
        ('body','在合成理论标签上，第四轮已知型号新状态测试Accuracy为100.00%、Macro-F1为1.0000；同架构未见RTX 6000 Ada测试Accuracy为96.70%、Macro-F1为0.9547。完全留出架构时仍存在覆盖不足，不能仅凭原始准确率认定未知GPU可靠。冻结部署模型为120棵树，模型版本rf-v4-known-states-ee35cfabe30f；后续集成和真实实验均未重新训练或调整分类阈值。'),
        ('sub','4. 可复用接口与Volcano设备级集成'),
        ('body','已实现classify_gpu()、rank_gpus()及Pod YAML解析，先检查后端、设备健康、显存、算力、实例数与多设备约束，再按任务类型评分。Python常驻服务启动时加载一次joblib；Go端设置超时、校验模型版本和UUID，遇到异常或待验证设备时保守回退。Python只提供预测建议，最终合法性与配额扣减仍由Volcano维护。'),
        ('body','在独立Volcano v1.13.1源码中新增默认关闭的rf-job，接入FilterNode、ScoreNode、Allocate和SubResource。RF排序实际进入物理GPU选择及UUID注解写入，而非只影响节点分数；分配前重新检查当前账本。已保留原DQN代码、生产配置及二进制，并完成Linux下Go与Python测试及分配释放回归。'),
        ('sub','5. 调度仿真与学习项独立贡献'),
        ('body','已实现11张GPU（4090×4、5090×4、L40×3）的离散事件仿真，对比Binpack、静态RF和动态RF。四种负载各使用3个随机种子、每条轨迹120个Pod，执行时间模型与分类标签规则分离。在训练为主场景，动态策略较Binpack平均等待下降11.59%；显存密集场景训练完成时间反而上升1.86%，说明当前策略并非全面占优。上述均为合成仿真指标。'),
        ('body','原生Go流程与真实RF HTTP服务的180组配对消融中，保持Pod、资源快照、硬约束和启发式权重相同，仅开关学习项，最终UUID改变37组（20.56%），其中同节点变化5组、跨节点变化32组。待验证设备保守回退不计作学习收益。该实验使用fake-client与合成设备快照，证明学习项可影响决策，不证明真实任务性能提高。'),
        ('sub','6. 真实Kubernetes逻辑分配与动态类别切换'),
        ('body','在Kubernetes v1.35.x、Volcano v1.13.1及vGPU Device Plugin v1.11.0环境中，仅运行sleep容器。第七轮完成6个真实Pod分配，原生Allocate与插件UUID核对一致，并验证一次RF服务不可用时的回退。第八轮于2026年10月20日在t3dgq四张4090上完成同卡动态实验：先用3个满算力逻辑占位Pod限定唯一合法候选，再依次分配和释放计算密集型、显存密集型配额。'),
        ('body','目标设备为GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd。计算型Pod申请1000 MiB/60 cores，显存型Pod申请17564 MiB/5 cores；S2至S4使用64 MiB/1 core观测Pod触发分类，取证后立即删除。冻结RF在各次分配前快照上的实际结果如下。'),
    ]
    fill_block(progress, first)
    dynamic_table(progress)
    after = [
        ('body','表中为真实注册逻辑容量，模型按既有22888 MiB参考容量进行比例映射，原生准入仍使用24564 MiB账本。五个状态的原始RF输出与展示类别一致，均为暂定可用状态；模型、阈值和权重保持冻结。第八轮主成功实验8次分配的RF推荐、Volcano永久UUID注解及插件消费UUID全部一致，8次原生配额增量正确，观察到超分配0次；资源全部释放后恢复初始状态，原调度器和ResourceQuota均已恢复。另一次采集器失败尝试的2个Pod已清理，未计为成功轨迹。'),
        ('body','插件UUID依据Pod UID和容器对应的kubelet AllocateResponse核对，不仅依据Pod Running。账本证据区分原生分配对象快照与API注解重建值。vGPU-memory为逻辑配额，不代表调度时物理预分配等量显存；本实验不调用CUDA，不验证训练时间或推理延迟。由于同卡实验只有一个合法候选，不能用它证明异构设备优选收益。'),
        ('head','（二）重难点分析及拟采取的解决措施'),
        ('sub','1. 适配标签与真实能力之间的差距'),
        ('body','公开硬件规格可核实，但缺乏统一可比的真实训练、推理标签。当前通过明确参考负载和理论时间模型构造合成监督，解决流程验证问题，同时保留来源类型、假设及缺失值。后续将分类准确率、仿真收益和真实逻辑分配分别报告；未经实测校准，不作真实计算性能保证。'),
        ('sub','2. 未见型号泛化和信息不足处理'),
        ('body','随机划分资源状态可能高估跨型号能力，个别架构还缺少完整类别覆盖。已采用型号分组留出、相关验证覆盖检查及待验证回退，RF投票比例不直接解释为可靠性。下一步完善错误样本与覆盖率分析，不以调低门槛掩盖未知设备误判。'),
        ('sub','3. 跨语言决策与原生资源账本一致性'),
        ('body','模型评分与实际Allocate处于不同阶段，状态变化、超时或异常UUID均可能使建议失效。已保留原生硬约束、锁与回滚路径，分配前复核状态，并以Pod UID关联RF、Allocate、插件与释放证据。后续补充更长序列的状态变化和并发回归，避免维护第二套真实资源账本。'),
        ('sub','4. 公平性目标与当前验证范围衔接'),
        ('body','目前完成的是类型适配与设备级逻辑调度，不能将装箱或资源均衡直接称为多租户公平。开题中的公平性目标尚缺队列级独立证据。拟优先在现有仿真器补充FIFO、HRRN对照，统一等待、减速比、饥饿判据与Jain指数口径，再评估等待时间补偿机制；GNN/PPO及Ampha平台集成不列为已完成成果。'),
    ]
    for kind,text in after:
        paragraph(progress.add_paragraph(), text, 14, bold=kind in ('head','sub'),
                  indent=kind=='body', keep=kind in ('head','sub'),
                  before=5 if kind=='head' else 2 if kind=='sub' else 0, after=2)

    outcomes = [
        ('body','本阶段成果以软件原型、数据集及可复核实验材料为主，尚无可列入本表的已发表论文或已授权专利。'),
        ('body','1. 形成GPU硬件参数库、逐参数来源审计、合成状态数据集及训练/验证/测试划分记录，保存缺失值与规格口径。'),
        ('body','2. 完成冻结RandomForest模型、适配分类和候选GPU排序API，配套命令行示例、Pod YAML及异常回退。'),
        ('body','3. 完成Volcano rf-job源码、Go–Python协议、原生分配释放测试及180组RF消融报告。'),
        ('body','4. 完成三策略调度仿真、第七轮6个sleep Pod验证及第八轮8次主实验分配，归档真实UUID、五状态分类、账本增量和释放证据。'),
        ('body','5. 整理CSV、PNG/SVG图表、算法流程图和实验分析。保存模型版本、源码提交、二进制摘要及测试记录，支撑后续论文撰写与复现。'),
    ]
    fill_block(d.tables[2].cell(0,0), outcomes, 12)
    schedule = [
        ('body','2026年10月24日—11月6日：冻结当前可运行版本，复核真实五状态证据、来源和测试划分，整理系统演示与论文实验章节；补齐接口边界和失败案例说明。'),
        ('body','2026年11月7日—11月20日：在离线仿真中补充公平性专项评价，加入FIFO/HRRN对照与等待补偿分析，报告平均等待、减速比、饥饿率和Jain指数，并明确其与设备适配性的关系。'),
        ('body','2026年11月21日—12月15日：完成方法、实现与实验章节初稿，统一分类覆盖率、资源预留率和碎片化口径。若需新增真实异构验证，另行协调资源和授权，不运行未批准的GPU计算。'),
        ('body','2026年12月16日—2027年1月：根据导师意见补充对照与局限讨论，完成论文全文、复现脚本和成果材料；结合实际完成情况凝练可申请的知识产权内容。'),
        ('body','2027年2月—3月：按学院安排完成预审、格式审核和修改，形成终稿及答辩演示。以上为后续计划，未将待做实验或拟申请成果计入已完成结果。'),
    ]
    fill_block(d.tables[3].cell(0,0), schedule, 12)

    review = d.tables[4]
    for row,col,text in [
        (0,1,'张兵昇'),(0,6,'SA24218208'),(0,9,'男'),
        (1,1,'先进技术研究院'),(1,7,'计算机技术'),
        (2,1,'算力调度'),(2,7,'卢汉成（校内）\n杨春鲜（实践）'),
        (3,5,'☑ 专业学位硕士          □ 专业学位博士'),
        (4,5,TITLE),(5,5,'无'),
    ]:
        fill_cell(review.cell(row,col),text,12,True,1.1)

    # A compulsory trailing table paragraph must fit on the review page.
    for pp in [d.paragraphs[-1]]:
        paragraph(pp,'',size=1,indent=False,line=1)

    # Keep section headings with their content, and all three blank signature lines together.
    for idx in [46,48,49,51,52,53,54,55]:
        d.paragraphs[idx].paragraph_format.keep_with_next=True

    # Only two intentional package parts change. All other bytes are preserved.
    stream = io.BytesIO()
    d.save(stream)
    with zipfile.ZipFile(stream) as zz:
        document_xml = zz.read('word/document.xml')
    with zipfile.ZipFile(REFERENCE) as z:
        settings = etree.fromstring(z.read('word/settings.xml'))
        u = settings.find(qn('w:updateFields'))
        if u is None:
            u=OxmlElement('w:updateFields');settings.append(u)
        u.set(qn('w:val'),'true')
        settings_xml=etree.tostring(settings,encoding='UTF-8',xml_declaration=True,standalone=True)
        with zipfile.ZipFile(OUTPUT,'w') as out:
            for info in z.infolist():
                payload = document_xml if info.filename=='word/document.xml' else settings_xml if info.filename=='word/settings.xml' else z.read(info)
                out.writestr(info,payload)

    final = package_inventory(OUTPUT)
    changed = [part for part in baseline if baseline[part]!=final[part]]
    assert set(changed)=={'word/document.xml','word/settings.xml'},changed
    final_doc=Document(OUTPUT)
    assert len(final_doc.sections)==4
    assert [s._sectPr.xml for s in final_doc.sections]==[s._sectPr.xml for s in Document(REFERENCE).sections]
    assert final_doc.tables[0].cell(4,5).text==TITLE
    assert final_doc.tables[4].cell(4,5).text==TITLE
    assert final_doc.tables[4].cell(13,0)._tc.xml==Document(REFERENCE).tables[4].cell(13,0)._tc.xml
    assert final_doc.tables[4].cell(14,0)._tc.xml==Document(REFERENCE).tables[4].cell(14,0)._tc.xml
    source_text=(QA/'proposal-extracted.txt').read_text(encoding='utf-8')
    assert TITLE in source_text
    assert sha(retained)==sha(REFERENCE)
    receipt={'output':str(OUTPUT),'sha256':sha(OUTPUT),'title':TITLE,'source_template_sha256':sha(REFERENCE),
             'source_proposal_sha256':sha(PROPOSAL),'changed_parts':changed,'section_count':4,
             'advisor_and_review_sections_unchanged':True,'data_through':'2026-10-20','report_date':'2026-10-23',
             'progress_characters':sum(len(text) for _,text in first+after),'abstract_characters':len(abstract)}
    (QA/'authoring-checks.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()

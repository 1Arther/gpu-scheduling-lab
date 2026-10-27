from pathlib import Path
from zipfile import ZipFile
from io import BytesIO
import hashlib, json, shutil
from lxml import etree as E
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

BASE=Path(__file__).parent
SRC=Path('C:/Users/EDY/Desktop/中期报告_张兵昇_修订版.docx')
OUT=SRC.with_name(SRC.stem+'_格式修正.docx')
data=SRC.read_bytes()
if not (BASE/'source.docx').exists(): (BASE/'source.docx').write_bytes(data)
doc=Document(BytesIO(data))
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

def font(p, size, bold=None):
    for r in p.runs:
        r.font.name='Times New Roman'
        rp=r._r.get_or_add_rPr()
        rf=rp.find(qn('w:rFonts'))
        rf.set(qn('w:eastAsia'),'宋体')
        for a in ['asciiTheme','hAnsiTheme','eastAsiaTheme','cstheme']:
            rf.attrib.pop(qn('w:'+a),None)
        r.font.size=Pt(size); r.font.color.rgb=RGBColor(0,0,0)
        if bold is not None:r.bold=bold

def para(p,size,line,indent=0,align=WD_ALIGN_PARAGRAPH.JUSTIFY,after=2):
    font(p,size)
    f=p.paragraph_format
    f.left_indent=Pt(0); f.right_indent=Pt(0); f.first_line_indent=Pt(indent)
    f.space_before=Pt(0); f.space_after=Pt(after)
    f.line_spacing=Pt(line); f.line_spacing_rule=WD_LINE_SPACING.EXACTLY
    f.widow_control=True; p.alignment=align

def cells(t):
    seen=set()
    for row in t.rows:
        for c in row.cells:
            if c._tc not in seen:
                seen.add(c._tc); yield c

def margins(c,top=30,bottom=30,left=50,right=50):
    pr=c._tc.get_or_add_tcPr()
    old=pr.find(qn('w:tcMar'))
    if old is not None:pr.remove(old)
    mar=OxmlElement('w:tcMar')
    for side,val in [('top',top),('bottom',bottom),('left',left),('right',right)]:
        x=OxmlElement('w:'+side);x.set(qn('w:w'),str(val));x.set(qn('w:type'),'dxa');mar.append(x)
    pr.append(mar)

# Summary form: upright Chinese labels and normal English word spacing.
t=doc.tables[0]
for row,height in zip(t.rows,[32,30,61,65,30,35,185,26,27,34,66]):
    row.height=Pt(height)
for c in cells(t):
    margins(c,left=40,right=40)
    vertical=c._tc.xpath('./w:tcPr/w:textDirection')
    for x in vertical:x.set(qn('w:val'),'lrTb')
    for p in c.paragraphs:
        if vertical:para(p,12,16,align=WD_ALIGN_PARAGRAPH.CENTER,after=0)
for idx in [2,3]:
    for c in cells(t):
        for p in c.paragraphs:
            if '作品创作' in p.text or '学位论文' in p.text and ('□' in p.text or '☑' in p.text):
                para(p,10.5,16,align=WD_ALIGN_PARAGRAPH.LEFT,after=0)
for c in cells(t):
    for p in c.paragraphs:
        if 'Performance' in p.text or 'performance' in p.text or 'Scheduling' in p.text:
            para(p,11,14,align=WD_ALIGN_PARAGRAPH.LEFT,after=0)
        if len(p.text)>200:para(p,12,16,indent=24,after=0)
        if '专业学位' in p.text and ('硕士' in p.text or '博士' in p.text):
            para(p,10.5,15,align=WD_ALIGN_PARAGRAPH.CENTER,after=0)

# Main research section: consistent typography; preserve all prose and runs.
t=doc.tables[1]
for p in t.cell(0,0).paragraphs:
    if not p.text.strip():
        para(p,2,2,after=0);continue
    heading=(p.text.startswith('（') or (len(p.text)<65 and any(r.bold for r in p.runs)))
    para(p,14,21,indent=0 if heading else 28,align=WD_ALIGN_PARAGRAPH.LEFT if heading else WD_ALIGN_PARAGRAPH.JUSTIFY)
    p.paragraph_format.keep_with_next=heading
    if heading:font(p,14,True);p.paragraph_format.keep_together=True
for nt in t.cell(0,0).tables:
    for c in cells(nt):
        margins(c,35,35,45,45)
        for p in c.paragraphs:para(p,11,15,align=WD_ALIGN_PARAGRAPH.CENTER,after=0)
    for row in nt.rows:
        pr=row._tr.get_or_add_trPr();pr.append(OxmlElement('w:cantSplit'))
    nt.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))

for ti in [2,3]:
    for c in cells(doc.tables[ti]):
        margins(c,40,40,65,65)
        for p in c.paragraphs:
            if ti==3:p.style=doc.styles['Normal']
            para(p,12,18,indent=24)
            p.paragraph_format.keep_with_next=False
            for x in p._p.xpath('.//w:rPr/w:bdr | ./w:pPr/w:pBdr | .//w:rPr/w:shd | ./w:pPr/w:shd | .//w:rPr/w:highlight'):
                x.getparent().remove(x)

# Consistent review-form width and borders.
t=doc.tables[4]
scale=9524/9640
for x in t._tbl.xpath('./w:tblGrid/w:gridCol | .//w:tcPr/w:tcW'):
    if x.tag==qn('w:gridCol') or x.get(qn('w:type'))=='dxa':
        x.set(qn('w:w'),str(round(int(x.get(qn('w:w')))*scale)))
for x in t._tbl.xpath('.//w:tcBorders/* | .//w:tblBorders/*'):
    if x.get(qn('w:val')) not in ['nil','none']:x.set(qn('w:sz'),'4')

buf=BytesIO();doc.save(buf)
with ZipFile(BytesIO(buf.getvalue())) as z:edited=z.read('word/document.xml')
with ZipFile(BytesIO(data)) as z:original={n:z.read(n) for n in z.namelist()};infos=z.infolist()
before=E.fromstring(original['word/document.xml']);after=E.fromstring(edited)
assert before.xpath('//w:t/text()',namespaces=NS)==after.xpath('//w:t/text()',namespaces=NS),'Body text changed'
assert len(before.xpath('//w:sectPr',namespaces=NS))==len(after.xpath('//w:sectPr',namespaces=NS))
changes={'word/document.xml':edited}
for n in ['word/footer2.xml','word/footer3.xml','word/footer4.xml']:
    root=E.fromstring(original[n])
    for x in list(root):root.remove(x)
    p=E.SubElement(root,qn('w:p'));pr=E.SubElement(p,qn('w:pPr'))
    E.SubElement(pr,qn('w:jc')).set(qn('w:val'),'center')
    sp=E.SubElement(pr,qn('w:spacing'));sp.set(qn('w:before'),'0');sp.set(qn('w:after'),'0');sp.set(qn('w:line'),'240');sp.set(qn('w:lineRule'),'exact')
    field=E.SubElement(p,qn('w:fldSimple'));field.set(qn('w:instr'),' PAGE ')
    r=E.SubElement(field,qn('w:r'));rp=E.SubElement(r,qn('w:rPr'))
    rf=E.SubElement(rp,qn('w:rFonts'));rf.set(qn('w:ascii'),'Times New Roman');rf.set(qn('w:hAnsi'),'Times New Roman')
    E.SubElement(rp,qn('w:sz')).set(qn('w:val'),'20')
    E.SubElement(r,qn('w:t')).text='1'
    changes[n]=E.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
root=E.fromstring(original['word/settings.xml'])
upd=root.find(qn('w:updateFields'))
if upd is None:upd=E.SubElement(root,qn('w:updateFields'))
upd.set(qn('w:val'),'true')
changes['word/settings.xml']=E.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
with ZipFile(OUT,'w') as z:
    for info in infos:z.writestr(info,changes.get(info.filename,original[info.filename]))
assert SRC.read_bytes()==data
audit={'source_sha256':hashlib.sha256(data).hexdigest(),'output_sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),'body_text_exactly_preserved':True,'changed_parts':list(changes),'output':str(OUT)}
(BASE/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(audit,ensure_ascii=False,indent=2))

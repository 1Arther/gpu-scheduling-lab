from pathlib import Path
import sys,json,re
import pypdfium2 as pdfium
import pdfplumber
from PIL import Image,ImageOps,ImageDraw
BASE=Path(__file__).parent
name=sys.argv[1] if len(sys.argv)>1 else 'qa-v1.pdf'
pdf=pdfium.PdfDocument(str(BASE/name));out=BASE/name.replace('.pdf','-pages');out.mkdir(exist_ok=True)
titles=[x[2:] for x in (BASE/'book.txt').read_text(encoding='utf-8').splitlines() if x.startswith('# ')]
mapping={};report=[]
with pdfplumber.open(BASE/name) as p:
    for i,page in enumerate(p.pages):
        txt=page.extract_text() or '';norm=re.sub(r'\s+','',txt)
        for title in titles:
            # Only a chapter heading in the first content lines is a chapter start, not the TOC.
            head=''.join(c['text'] for c in page.chars if c['size']>=17)
            head=re.sub(r'[^\u4e00-\u9fff]','',head)
            if i>1 and re.sub(r'[^\u4e00-\u9fff]','',title) in head and title not in mapping:mapping[title]=i+1
        report.append({'page':i+1,'chars':len(txt),'start':txt[:100],'end':txt[-100:]})
        pdf[i].render(scale=1.5).to_pil().save(out/f'page-{i+1:02d}.png')
for k in range(0,len(pdf),6):
    sheet=Image.new('RGB',(1275,1740),'#dddddd');draw=ImageDraw.Draw(sheet)
    for j in range(6):
        if k+j>=len(pdf):break
        im=Image.open(out/f'page-{k+j+1:02d}.png');im.thumbnail((415,555))
        x=(j%3)*425;y=(j//3)*870
        # Larger page contact grid: reserve readable header then fit full page.
        sheet.paste(im,(x,y+25));draw.text((x+10,y+5),f'PAGE {k+j+1}',fill='black')
    sheet.save(out/f'contact-{k//6+1:02d}.png')
(BASE/'page-map.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding='utf-8')
(out/'page-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'pages':len(pdf),'chapter_pages':mapping,'missing':[x for x in titles if x not in mapping],'short_pages':[r for r in report if r['chars']<200]},ensure_ascii=False,indent=2))

# 中期报告模板执行合同

Reference: C:\Users\EDY\Desktop\中期报告.docx; retained copy: C:\Users\EDY\Documents\ChatGPT\vGPU调度\gpu-scheduling-lab\reports\midterm-20261023\中期报告_原始模板.docx
SHA256: c7bd314e9dcf499e4c930f8c6ce896068a0a34ee6b9f32bcdba31682048512a7. Original proposal SHA256: fca644ab2c67c13290b028e2a70d8573c019728a3ebedd15ff24db681b071091.
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

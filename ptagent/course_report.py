"""Word report generated from observations, never from invented outcomes."""
from pathlib import Path
import json

from docx import Document
from docx.shared import Cm, Pt, RGBColor


LABELS = {'passed': 'Пройдено', 'failed': 'Не пройдено', 'unverified': 'Не подтверждено'}


def write_course_report(folder: Path, report, observations):
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.3); section.right_margin = Cm(2)
    normal = document.styles['Normal']
    normal.font.name = 'Calibri'; normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    document.styles['Title'].font.color.rgb = RGBColor(0, 0, 0)
    document.add_paragraph(report.title.replace('.', ' '), 'Title')
    passed = sum(c.status == 'passed' for c in report.checks)
    document.add_paragraph(f'Подтверждено требований: {passed} из {len(report.checks)}. '
                           'Отчёт содержит фактические ответы Packet Tracer. Неподтверждённые этапы перечислены отдельно.')
    document.add_paragraph(f'Начало: {report.started_at}. Окончание: {report.finished_at}.')
    document.add_heading('Проверка требований', level=1)
    for check in report.checks:
        paragraph = document.add_paragraph()
        paragraph.add_run(f'{check.description} — {LABELS[check.status]}. ').bold = True
        paragraph.add_run(check.evidence)
    if report.errors:
        document.add_heading('Ошибки и ограничения', level=1)
        for error in report.errors:
            document.add_paragraph(error)
    document.add_heading('Ход экспериментов', level=1)
    for observation in observations:
        document.add_heading(observation['title'], level=2)
        if 'image' in observation:
            path = folder / observation['image']
            if path.is_file():
                document.add_picture(str(path), width=Cm(16))
        else:
            text = observation.get('evidence', '')
            try:
                data=json.loads(text)
            except (ValueError,TypeError):
                data=None
            if isinstance(data,dict) and isinstance(data.get('trace'),list):
                frames=data['trace']
                document.add_paragraph(data.get('summary',f'Наблюдалось событий: {len(frames)}'))
                for frame in frames:
                    if frame.get('traffic_type') not in ('ARP','ICMP','STP','OSPF','RIPv1','RIPv2'):
                        continue
                    document.add_paragraph(f"Событие {frame.get('index')}  {frame.get('traffic_type')}  "
                        f"{frame.get('device')}  {frame.get('in_port') or '—'} → {frame.get('out_port') or '—'}  "
                        f"{frame.get('status') or ''}  время {frame.get('sim_time')}")
                example=next((f for f in frames if f.get('traffic_type')=='ARP' and f.get('decisions')),None)
                if example:
                    document.add_paragraph('Пример содержимого и обработки ARP пакета')
                    for decision in example['decisions']:
                        document.add_paragraph(f"OSI {decision.get('layer')}: {decision.get('description','')}")
                document.add_paragraph('Полные поля пакетов и решения OSI каждого события сохранены в observations.json.')
                continue
            # Keep the Word report readable. Full unabridged evidence is alongside it.
            if len(text) > 14000:
                text = text[:14000] + '\n[Продолжение в observations.json и evidence-файлах]'
            for line in text.splitlines():
                paragraph = document.add_paragraph(line)
                paragraph.paragraph_format.space_after = Pt(1)
                for run in paragraph.runs:
                    run.font.name = 'Consolas'; run.font.size = Pt(8)
    document.save(folder / 'report.docx')

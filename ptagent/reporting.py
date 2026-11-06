from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .models import RunReport


def error_message(error:BaseException) -> str:
    if isinstance(error,BaseExceptionGroup):
        return "; ".join(error_message(e) for e in error.exceptions)
    return redact(str(error))


def redact(text: str) -> str:
    text = re.sub(r"(?im)(\b(?:enable\s+secret|password|secret|authorization\s*:)\s+)[^\r\n]+",r"\1[скрыто]",text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[ключ скрыт]",text)
    return text


def write_report(folder:Path, report:RunReport):
    folder.mkdir(parents=True,exist_ok=True)
    # Redact string values, not JSON syntax, to keep a machine-readable artifact.
    def clean(value):
        if isinstance(value,str):
            return redact(value)
        if isinstance(value,list):
            return [clean(v) for v in value]
        if isinstance(value,dict):
            return {k:clean(v) for k,v in value.items()}
        return value
    data = clean(report.model_dump())
    temp = folder / "report.json.tmp"
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    temp.replace(folder/"report.json")
    esc = lambda s:html.escape(str(s))
    labels = {"passed":"Пройдено", "failed":"Ошибка", "unverified":"Не подтверждено"}
    rows = "".join(f'<tr><td>{esc(c.id)}</td><td>{esc(c.description)}</td><td class="{c.status}">{labels[c.status]}</td><td><pre>{esc(redact(c.evidence))}</pre></td><td>{c.attempt}</td></tr>' for c in report.checks)
    status = {"running":"Выполняется", "completed":"Завершено", "failed":"Завершено с ошибками", "stopped":"Остановлено", "planned":"План подготовлен; Cisco не запускался"}[report.status]
    errors = "".join(f"<li>{esc(redact(e))}</li>" for e in report.errors)
    assumptions = "".join(f"<li>{esc(s)}</li>" for s in report.assumptions)
    page = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Отчёт — {esc(report.title)}</title>
<style>body{{font:15px 'Segoe UI',sans-serif;background:#f3f6fb;color:#18263b;margin:40px}}main{{background:white;padding:32px;border-radius:16px;max-width:1200px;margin:auto}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #ddd;vertical-align:top}}pre{{white-space:pre-wrap;word-break:break-word;max-width:640px}}.passed{{color:#137348}}.failed{{color:#b42318}}.unverified{{color:#936000}}</style>
<main><h1>{esc(report.title)}</h1><p>{esc(status)}</p><p>Начало: {esc(report.started_at)} · Окончание: {esc(report.finished_at or '—')}</p>
<p>Проект: {esc(report.project or 'Не сохранён')}</p><h2>Проверки</h2><table><tr><th>ID</th><th>Требование</th><th>Результат</th><th>Наблюдение</th><th>Попытка</th></tr>{rows}</table>
<h2>Ошибки и ограничения</h2><ul>{errors}</ul><h2>Принятые допущения</h2><ul>{assumptions}</ul><h2>Среда</h2><pre>{esc(json.dumps(report.environment,ensure_ascii=False,indent=2))}</pre></main></html>'''
    (folder/"report.html").write_text(page,encoding="utf-8")

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QPainter, QPen, QPixmap, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QFrame, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget, QSpinBox,
)

from .bridge import connect_bridge
from .control import RunControl, Stopped
from .desktop import WindowsDesktop
from .documents import Assignment, load_assignment
from .environment import find_packet_tracer, inspect_environment
from .executor import Executor, now
from .models import LabPlan, RunReport
from .planner import analyze
from .reporting import error_message, redact, write_report


STYLE = """
QMainWindow,QDialog {background:#0d1424;color:#e8edf7}
QWidget {font-family:'Segoe UI';font-size:13px;color:#dce5f5}
QFrame#card {background:#151f33;border:1px solid #27344b;border-radius:14px}
QLabel#eyebrow {color:#70dbce;font-size:11px;font-weight:700;letter-spacing:2px}
QLabel#heading {font-size:26px;font-weight:700;color:#f6f8ff}
QLabel#muted {color:#95a5c0}
QLabel#stage {color:#f6f8ff;font-size:17px;font-weight:600}
QPushButton {background:#23314a;border:1px solid #35435f;border-radius:8px;padding:10px 16px;font-weight:600}
QPushButton:hover {background:#304360;border-color:#6385ae}
QPushButton:disabled {color:#687891;background:#182338;border-color:#28344a}
QPushButton#primary {background:#50d4bb;color:#09221e;border:none}
QPushButton#primary:hover {background:#81e6d3}
QPushButton#stop {color:#ffaaaa}
QPlainTextEdit,QTextBrowser,QLineEdit,QSpinBox {background:#101a2d;border:1px solid #2b3b57;border-radius:8px;padding:10px;selection-background-color:#315d7b}
QPlainTextEdit {font-family:'Cascadia Mono','Consolas';font-size:12px}
QProgressBar {border:0;border-radius:5px;background:#26334a;height:10px;text-align:center;color:transparent}
QProgressBar::chunk {background:#50d4bb;border-radius:5px}
QTabWidget::pane {border:0}
QTabBar::tab {background:#151f33;color:#91a2be;padding:12px 18px;border-bottom:2px solid transparent}
QTabBar::tab:selected {color:#71e3cf;border-bottom:2px solid #50d4bb}
QGraphicsView {background:#101a2d;border:1px solid #2b3b57;border-radius:10px}
QSplitter::handle {background:#0d1424;width:12px}
QCheckBox {spacing:8px}
QCheckBox::indicator {width:17px;height:17px}
QToolTip {background:#26334a;color:white;border:1px solid #405778;padding:6px}
"""


def initialize_fonts():
    # The Windows offscreen Qt platform has no system font enumeration.
    # Explicitly register existing Windows fonts, without installing/changing fonts.
    folder=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"
    for name in ("segoeui.ttf","seguisb.ttf","consola.ttf"):
        path=folder/name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


class TopologyView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.plan=None
        self.placeholder()

    def placeholder(self):
        text=self.scene().addText("Здесь появится план сети\nпосле анализа задания",QFont("Segoe UI",14))
        text.setDefaultTextColor(QColor("#7e91b0"))

    def set_plan(self,plan:LabPlan):
        self.plan=plan
        scene=self.scene()
        scene.clear()
        positions={d.name:(d.x,d.y) for d in plan.devices}
        for link in plan.links:
            a,b=positions[link.a.device],positions[link.b.device]
            scene.addLine(a[0]+55,a[1]+25,b[0]+55,b[1]+25,QPen(QColor("#527597"),2))
            short=lambda port:port.replace("GigabitEthernet","Gi").replace("FastEthernet","Fa").replace("Ethernet","Eth")
            label=scene.addText(f"{short(link.a.port)} ↔ {short(link.b.port)}",QFont("Segoe UI",8))
            label.setDefaultTextColor(QColor("#89a9c7"))
            label.setPos((a[0]+b[0])/2+8,(a[1]+b[1])/2+32)
        colors={"router":"#50d4bb","switch":"#72a7ff","pc":"#c2a8ff","server":"#f9c078","other":"#9ba8bb"}
        for d in plan.devices:
            color=QColor(colors[d.kind])
            scene.addRect(d.x,d.y,110,50,QPen(color,1.5),QColor("#1d2e46"))
            name=scene.addText(d.name,QFont("Segoe UI",12,QFont.Weight.Bold))
            name.setDefaultTextColor(color)
            name.setPos(d.x+8,d.y+3)
            model=scene.addText(d.model,QFont("Segoe UI",8))
            model.setDefaultTextColor(QColor("#b8c6dd"))
            model.setPos(d.x+8,d.y+27)
            addresses="\n".join(i.address for i in d.interfaces if i.address)
            if addresses:
                ip=scene.addText(addresses,QFont("Consolas",9))
                ip.setDefaultTextColor(QColor("#b8c6dd"))
                ip.setPos(d.x,d.y+52)
        self.fitInView(scene.itemsBoundingRect().adjusted(-45,-40,45,40),Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.scene().items():
            self.fitInView(self.scene().itemsBoundingRect().adjusted(-40,-40,40,40),Qt.AspectRatioMode.KeepAspectRatio)


class DocumentWorker(QThread):
    loaded=Signal(object)
    error=Signal(str)

    def __init__(self,path):
        super().__init__()
        self.path=path

    def run(self):
        try:
            self.loaded.emit(load_assignment(self.path))
        except Exception as exc:
            self.error.emit(str(exc))


class AgentWorker(QThread):
    log=Signal(str)
    stage=Signal(str,int)
    plan_ready=Signal(object)
    result=Signal(object,str)

    def __init__(self,assignment,folder,use_ai,plan_only,control,course_request=None):
        super().__init__()
        self.assignment,self.folder=assignment,folder
        self.use_ai,self.plan_only,self.control=use_ai,plan_only,control
        self.course_request=course_request

    def run(self):
        try:
            asyncio.run(self.execute())
        except BaseException as exc:
            # ExceptionGroup from the MCP task group still becomes a visible failure report.
            error=error_message(exc)
            report=RunReport(title="Выполнение лабораторной",started_at=now(),finished_at=now(),status="failed",errors=[error])
            write_report(self.folder,report)
            self.log.emit("Ошибка: "+redact(error))
            self.result.emit(report,str(self.folder))

    async def execute(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        self.stage.emit("Анализ задания",5)
        self.log.emit("Извлечение требований и проверка согласованности")
        environment=inspect_environment()
        desktop=None
        if not self.plan_only:
            desktop=require_open_cisco()
        if self.course_request is not None:
            from .coursework import REQUIREMENTS,TITLES
            from .course_runner import CourseRunner
            request=self.course_request
            if request.lab==3:
                from .course_switching import switching_plan
                self.plan_ready.emit(switching_plan(request.variant)[0])
            if self.plan_only:
                report=RunReport(title=TITLES[request.lab],started_at=now(),finished_at=now(),status='planned',
                    assumptions=REQUIREMENTS[request.lab],environment=environment)
                write_report(self.folder,report)
            else:
                async with connect_bridge(self.folder,self.log.emit) as bridge:
                    report=await CourseRunner(bridge,self.folder,self.control,self.stage.emit,self.log.emit).run_course(request,environment,desktop)
            self.result.emit(report,str(self.folder))
            return
        try:
            await self.control.checkpoint()
            plan=await analyze(self.assignment,self.use_ai)
            await self.control.checkpoint()
        except Stopped:
            report=RunReport(title="Выполнение лабораторной",started_at=now(),finished_at=now(),status="stopped",environment=environment)
            write_report(self.folder,report)
            self.result.emit(report,str(self.folder))
            return
        self.plan_ready.emit(plan)
        (self.folder/"plan.json").write_text(plan.model_dump_json(indent=2),encoding="utf-8")
        if plan.unresolved:
            raise ValueError("Не удалось разрешить неоднозначность: "+"; ".join(plan.unresolved))
        self.log.emit(f"План: {len(plan.devices)} устройств, {len(plan.links)} соединений, {len(plan.checks)} проверок")
        if self.plan_only:
            report=RunReport(title=plan.title,started_at=now(),finished_at=now(),status="planned",environment=environment,assumptions=plan.assumptions)
            write_report(self.folder,report)
            self.result.emit(report,str(self.folder))
            return
        self.log.emit("Подключение к уже открытому Cisco Packet Tracer")
        await self.control.checkpoint()
        desktop.require_running()
        async with connect_bridge(self.folder,self.log.emit) as bridge:
            report=await Executor(bridge,self.folder,self.control,self.stage.emit,self.log.emit).run(plan,environment,desktop)
        self.result.emit(report,str(self.folder))


def require_open_cisco():
    exe=find_packet_tracer()
    if not exe:
        raise RuntimeError("Cisco Packet Tracer не найден. Укажите PACKET_TRACER_EXE. Автозапуск Cisco отключён.")
    desktop=WindowsDesktop(exe)
    desktop.require_running()
    return desktop


class OpenProjectWorker(QThread):
    log=Signal(str)
    error=Signal(str)

    def __init__(self,project,folder):
        super().__init__()
        self.project,self.folder=Path(project),Path(folder)

    def run(self):
        try:
            asyncio.run(self.execute())
        except Exception as exc:
            self.error.emit(redact(error_message(exc)))

    async def execute(self):
        desktop=require_open_cisco()
        if not self.project.is_file():
            raise RuntimeError("Готовый проект не найден")
        self.folder.mkdir(parents=True,exist_ok=True)
        async with connect_bridge(self.folder,self.log.emit) as bridge:
            desktop.require_running()
            await bridge.call("pt_bridge_status")
            backup=self.folder/"before-open.pkt"
            await bridge.call("pt_save_project",filename=backup.name,directory=str(self.folder.resolve()),overwrite=False)
            if not backup.is_file() or backup.stat().st_size==0:
                raise RuntimeError("Не подтверждена резервная копия текущей сети; открытие отменено")
            desktop.require_running()
            await bridge.call("pt_open_project",path=str(self.project.resolve()))
            self.log.emit("Проект открыт в работающем Cisco")


class SetupDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение Cisco и ИИ")
        self.resize(720,540)
        layout=QVBoxLayout(self)
        text=QTextBrowser()
        text.setOpenExternalLinks(True)
        text.setHtml('''<h2>Однократное подключение Packet Tracer</h2>
<p>Приложение использует отдельный открытый мост <b>Mats2208/MCP-Packet-Tracer</b>. После подключения лабораторные выполняются автоматически.</p>
<ol><li>Запустите «Prepare.cmd»: он установит библиотеки в папку приложения.</li>
<li>Загрузите соответствующее установленной версии моста расширение .pts с <a href="https://github.com/Mats2208/MCP-Packet-Tracer/releases">официальной страницы проекта</a>.</li>
<li>В Cisco: Extensions → Scripting → Configure PT Script Modules → Add. Выберите .pts и запустите модуль. Вход в Cisco выполняет пользователь.</li></ol>
<p>Программа сама запускает мост при выполнении. Не запускайте второй экземпляр моста параллельно.</p>
<p><b>Cisco нужно открыть самостоятельно до начала выполнения. Программа никогда не запускает Cisco, включая кнопку открытия готового проекта.</b> Если Cisco закрыт или модуль не отвечает, операция останавливается.</p>
<h3>Анализ сложных заданий</h3><p>Для API задаются переменные окружения <b>LAB_AI_ENDPOINT</b> (полный URL Chat Completions), <b>LAB_AI_MODEL</b> и <b>LAB_AI_KEY</b>. Ключ не записывается в файлы приложения.</p>
<p>При включённом ИИ содержимое загруженного задания и изображения отправляются выбранному API. Подписка ChatGPT не заменяет API-доступ. Для структурированных TXT/JSON API не нужен.</p>
<p>Если Python моста находится отдельно: <b>PT_BRIDGE_PYTHON</b>. Если Cisco установлен в другом месте: <b>PACKET_TRACER_EXE</b>.</p>''')
        layout.addWidget(text)
        close=QPushButton("Понятно")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class CourseDialog(QDialog):
    def __init__(self,lab,parent=None):
        super().__init__(parent)
        from .coursework import TITLES
        self.lab=lab
        self.setWindowTitle(f'Лабораторная №{lab} — {TITLES[lab]}')
        self.resize(640,350)
        layout=QFormLayout(self)
        self.variant=QSpinBox(); self.variant.setRange(1,20)
        self.hostname=QLineEdit('Student-1-Group')
        self.previous=QLineEdit()
        self.topology_plan=QLineEdit()
        self.current=QCheckBox('Использовать уже открытую сеть предыдущей лабораторной')
        self.current.setChecked(True)
        profile_path=Path(os.environ.get('CISCO_AGENT_PROFILE',str(Path.home()/'Documents'/'PacketTracerLabs'/'course-user.local.json')))
        try:
            profile=json.loads(profile_path.read_text(encoding='utf-8'))
            self.variant.setValue(int(profile.get('variant',1)))
            self.hostname.setText(profile.get('hostname','Student-1-Group'))
            previous=profile.get('previous_projects',{}).get(str(lab),'')
            if previous and Path(previous).is_file():
                self.previous.setText(previous);self.current.setChecked(False)
        except (OSError,ValueError,TypeError):
            pass
        self.password=QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Пусто — сгенерировать учебный пароль для этого запуска')
        layout.addRow('Вариант / номер по списку',self.variant)
        if lab==3:
            layout.addRow('Имя IOS латиницей: ФамилияИО-номер-группа',self.hostname)
            layout.addRow('Учебный пароль console/vty/enable',self.password)
        elif lab==5:
            row=QHBoxLayout();row.addWidget(self.topology_plan)
            choose=QPushButton('Выбрать JSON схемы');row.addWidget(choose)
            choose.clicked.connect(self.choose_topology)
            layout.addRow('Точная схема варианта (LabPlan JSON)',row)
            warning=QLabel('В PDF №5 схемы находятся по внешним ссылкам. Нужен план именно вашего варианта; случайная сеть не подставляется.')
            warning.setWordWrap(True);layout.addRow(warning)
        else:
            row=QHBoxLayout(); row.addWidget(self.previous)
            choose=QPushButton('Выбрать .pkt'); row.addWidget(choose)
            choose.clicked.connect(self.choose_project)
            layout.addRow('Готовая сеть из предыдущей работы',row)
            layout.addRow(self.current)
        self.physical=QCheckBox('Выполнить опыт Windows ARP: связь со шлюзом временно пропадёт, затем восстановится')
        self.interface_index=QSpinBox();self.interface_index.setRange(0,10000)
        self.interface_index.setSpecialValueText('Единственный адаптер со шлюзом')
        if lab==2:
            layout.addRow(self.physical)
            layout.addRow('Индекс адаптера Windows для опыта',self.interface_index)
        text=QLabel('Сценарий выполняется без ИИ API. Перед изменениями сохраняется резервная копия. '
                    'Отчёт Word содержит фактические проверки и неподтверждённые этапы.')
        text.setWordWrap(True);layout.addRow(text)
        buttons=QHBoxLayout();accept=QPushButton('Выполнить сценарий');cancel=QPushButton('Отмена')
        accept.clicked.connect(self.validate_and_accept);cancel.clicked.connect(self.reject)
        buttons.addWidget(accept);buttons.addWidget(cancel);layout.addRow(buttons)

    def choose_project(self):
        path,_=QFileDialog.getOpenFileName(self,'Сеть предыдущей лабораторной','','Packet Tracer (*.pkt)')
        if path:self.previous.setText(path);self.current.setChecked(False)

    def choose_topology(self):
        path,_=QFileDialog.getOpenFileName(self,'Точная схема варианта №5','','План LabPlan (*.json)')
        if path:self.topology_plan.setText(path)

    def request(self):
        from .coursework import CourseRequest
        return CourseRequest(lab=self.lab,variant=self.variant.value(),hostname=self.hostname.text().strip(),
            previous_project=self.previous.text().strip(),use_current=self.current.isChecked(),
            physical_windows=self.physical.isChecked(),physical_interface=self.interface_index.value(),
            console_password=self.password.text(),enable_password=self.password.text(),topology_plan=self.topology_plan.text().strip())

    def validate_and_accept(self):
        try:self.request()
        except ValueError as exc:
            QMessageBox.warning(self,'Проверьте параметры',str(exc));return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CiscoAgent 0.2.1 — Packet Tracer")
        self.resize(1390,900)
        self.setMinimumSize(1050,700)
        self.assignment=Assignment("")
        self.worker=None
        self.loader=None
        self.opener=None
        self.report=None
        self.result_folder=None
        self.close_when_finished=False
        central=QWidget()
        self.setCentralWidget(central)
        root=QVBoxLayout(central)
        root.setContentsMargins(28,22,28,22)
        root.setSpacing(18)
        header=QHBoxLayout()
        titles=QVBoxLayout()
        eyebrow=QLabel("CISCO  /  LAB AUTOMATION")
        eyebrow.setObjectName("eyebrow")
        titles.addWidget(eyebrow)
        title=QLabel("Лабораторная — от задания до сети")
        title.setObjectName("heading")
        titles.addWidget(title)
        sub=QLabel("Работает только с уже открытым Cisco. Загрузите задание и начните выполнение.")
        sub.setObjectName("muted")
        titles.addWidget(sub)
        header.addLayout(titles,1)
        setup=QPushButton("Подключение Cisco и ИИ")
        setup.clicked.connect(lambda:SetupDialog(self).exec())
        header.addWidget(setup)
        root.addLayout(header)
        split=QSplitter(Qt.Orientation.Horizontal)
        left=QFrame()
        left.setObjectName("card")
        left_layout=QVBoxLayout(left)
        left_layout.setContentsMargins(18,18,18,18)
        row=QHBoxLayout()
        row.addWidget(QLabel("01   ЗАДАНИЕ"),1)
        self.load=QPushButton("Загрузить лабораторную")
        self.load.clicked.connect(self.load_file)
        row.addWidget(self.load)
        left_layout.addLayout(row)
        self.source=QLabel("PDF · DOCX · PNG · JPG · TXT")
        self.source.setObjectName("muted")
        self.source.setWordWrap(True)
        left_layout.addWidget(self.source)
        self.editor=QPlainTextEdit()
        self.editor.setTabChangesFocus(True)
        self.editor.setPlaceholderText("Загрузите файл или вставьте текст задания…\n\nБез API можно использовать пример из папки examples.")
        left_layout.addWidget(self.editor,3)
        self.image=QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMaximumHeight(180)
        self.image.hide()
        left_layout.addWidget(self.image)
        self.ai=QCheckBox("Использовать ИИ для текста и изображений")
        self.ai.setToolTip("Задание будет отправлено API из LAB_AI_ENDPOINT. Ключ: LAB_AI_KEY.")
        self.plan_only=QCheckBox("Только подготовить план — без изменения Cisco")
        left_layout.addWidget(self.ai)
        left_layout.addWidget(self.plan_only)
        split.addWidget(left)
        right=QFrame()
        right.setObjectName("card")
        right_layout=QVBoxLayout(right)
        right_layout.setContentsMargins(18,18,18,18)
        right_layout.addWidget(QLabel("02   ТОПОЛОГИЯ И РЕЗУЛЬТАТ"))
        self.tabs=QTabWidget()
        self.graph=TopologyView()
        self.plan_text=QPlainTextEdit()
        self.plan_text.setReadOnly(True)
        self.report_view=QTextBrowser()
        self.report_view.setOpenExternalLinks(True)
        self.tabs.addTab(self.graph,"План сети")
        self.tabs.addTab(self.plan_text,"Параметры")
        self.tabs.addTab(self.report_view,"Проверки")
        right_layout.addWidget(self.tabs)
        caption=QLabel("Предварительный план; фактические результаты появятся после проверки в Cisco.")
        caption.setObjectName("muted")
        caption.setWordWrap(True)
        right_layout.addWidget(caption)
        split.addWidget(right)
        split.setSizes([550,750])
        root.addWidget(split,4)
        status=QFrame()
        status.setObjectName("card")
        status_layout=QVBoxLayout(status)
        status_layout.setContentsMargins(18,14,18,14)
        progress_row=QHBoxLayout()
        self.stage_label=QLabel("Готов к работе")
        self.stage_label.setObjectName("stage")
        self.percent=QLabel("0%")
        self.percent.setObjectName("muted")
        progress_row.addWidget(self.stage_label,1)
        progress_row.addWidget(self.percent)
        status_layout.addLayout(progress_row)
        self.progress=QProgressBar()
        self.progress.setRange(0,100)
        self.progress.setValue(0)
        status_layout.addWidget(self.progress)
        self.logs=QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(3000)
        self.logs.setMaximumHeight(130)
        status_layout.addWidget(self.logs)
        root.addWidget(status,1)
        actions=QHBoxLayout()
        self.start=QPushButton("Начать выполнение")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.start_run)
        self.pause=QPushButton("Пауза")
        self.pause.clicked.connect(self.pause_run)
        self.resume=QPushButton("Продолжить")
        self.resume.clicked.connect(self.resume_run)
        self.stop=QPushButton("Остановить")
        self.stop.setObjectName("stop")
        self.stop.clicked.connect(self.stop_run)
        self.open_project=QPushButton("Открыть готовый проект")
        self.open_project.clicked.connect(self.open_result)
        self.open_report=QPushButton("Открыть отчёт")
        self.open_report.clicked.connect(self.open_report_file)
        for widget in (self.start,self.pause,self.resume,self.stop):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.open_report)
        actions.addWidget(self.open_project)
        root.addLayout(actions)
        self.set_busy(False)
        self.open_project.setEnabled(False)
        self.open_report.setEnabled(False)
        self.load.setToolTip("Загрузить задание · Ctrl+O")
        self.start.setToolTip("Начать выполнение · Ctrl+Enter")
        self.shortcuts=[QShortcut(QKeySequence("Ctrl+O"),self),QShortcut(QKeySequence("Ctrl+Return"),self)]
        self.shortcuts[0].activated.connect(lambda:self.load.click() if self.load.isEnabled() else None)
        self.shortcuts[1].activated.connect(lambda:self.start.click() if self.start.isEnabled() else None)
        self.add_log("Готово. Cisco должен быть уже открыт. Автозапуск Cisco отключён.")

    def set_busy(self,busy):
        for widget in (self.start,self.load,self.editor,self.ai,self.plan_only):
            widget.setEnabled(not busy)
        self.pause.setEnabled(busy)
        self.stop.setEnabled(busy)
        self.resume.setEnabled(False)
        if busy:
            self.open_project.setEnabled(False)
            self.open_report.setEnabled(False)

    def add_log(self,text):
        line=f"{datetime.now():%H:%M:%S}  {redact(text)}"
        self.logs.appendPlainText(line)
        if self.result_folder:
            with (self.result_folder/"actions.log").open("a",encoding="utf-8") as log:
                log.write(line+"\n")

    def load_file(self):
        path,_=QFileDialog.getOpenFileName(self,"Загрузить лабораторную","","Задания (*.pdf *.docx *.png *.jpg *.jpeg *.txt *.json)")
        if not path:
            return
        self.load.setEnabled(False)
        self.start.setEnabled(False)
        self.loader=DocumentWorker(Path(path))
        self.loader.loaded.connect(self.show_assignment)
        self.loader.error.connect(lambda e:QMessageBox.warning(self,"Не удалось загрузить",e))
        self.loader.finished.connect(lambda:(self.load.setEnabled(True),self.start.setEnabled(True)))
        self.loader.start()

    def show_assignment(self,assignment):
        self.assignment=assignment
        self.editor.setPlainText(assignment.text)
        self.source.setText(Path(assignment.source).name+f" · изображений/страниц: {len(assignment.images)}")
        self.source.setToolTip(assignment.source)
        if assignment.course_id:
            from .coursework import TITLES
            self.source.setText(self.source.text()+f' · Сценарий №{assignment.course_id}: {TITLES[assignment.course_id]} · API не нужен')
        if assignment.images:
            pixmap=QPixmap()
            pixmap.loadFromData(assignment.images[0])
            self.image.setPixmap(pixmap.scaled(500,175,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
            self.image.show()
        else:
            self.image.hide()

    def start_run(self):
        text=self.editor.toPlainText().strip()
        if not text and not self.assignment.images:
            QMessageBox.information(self,"Нужно задание","Загрузите файл или вставьте текст задания.")
            return
        course_request=None
        if self.assignment.course_id:
            dialog=CourseDialog(self.assignment.course_id,self)
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            course_request=dialog.request()
        self.control=RunControl()
        base=Path(os.environ.get("PT_AGENT_RESULTS",str(Path.home()/"Documents"/"PacketTracerLabs")))
        self.result_folder=base/(datetime.now().strftime("%Y-%m-%d_%H-%M-%S")+"_"+uuid.uuid4().hex[:6])
        self.result_folder.mkdir(parents=True,exist_ok=False)
        self.logs.clear()
        self.report=None
        self.set_busy(True)
        self.progress.setValue(0)
        self.worker=AgentWorker(Assignment(text,self.assignment.images,self.assignment.source,self.assignment.course_id),self.result_folder,self.ai.isChecked(),self.plan_only.isChecked(),self.control,course_request)
        self.worker.log.connect(self.add_log)
        self.worker.stage.connect(self.show_stage)
        self.worker.plan_ready.connect(self.show_plan)
        self.worker.result.connect(self.show_result)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def show_stage(self,stage,percent):
        self.stage_label.setText(stage)
        self.progress.setValue(percent)
        self.percent.setText(f"{percent}%")

    def show_plan(self,plan):
        self.graph.set_plan(plan)
        self.plan_text.setPlainText(plan.model_dump_json(indent=2))

    def show_result(self,report,folder):
        self.report=report
        self.result_folder=Path(folder)
        labels={"completed":"Завершено", "failed":"Завершено с ошибками", "stopped":"Остановлено", "planned":"План подготовлен"}
        self.stage_label.setText(labels.get(report.status,report.status))
        self.add_log(labels.get(report.status,report.status)+". Отчёт: "+str(self.result_folder/"report.html"))
        self.report_view.setSource(QUrl.fromLocalFile(str(self.result_folder/"report.html")))
        self.tabs.setCurrentWidget(self.report_view)

    def worker_finished(self):
        self.set_busy(False)
        self.open_project.setEnabled(bool(self.report and self.report.project))
        self.open_report.setEnabled(bool(self.result_folder and (self.result_folder/"report.html").exists()))
        if self.close_when_finished:
            self.close()

    def pause_run(self):
        self.control.pause()
        self.pause.setEnabled(False)
        self.resume.setEnabled(True)
        self.add_log("Пауза запрошена. Уже отправленная операция Cisco завершится перед паузой.")

    def resume_run(self):
        self.control.resume()
        self.pause.setEnabled(True)
        self.resume.setEnabled(False)
        self.add_log("Выполнение продолжено")

    def stop_run(self):
        self.control.stop()
        self.stop.setEnabled(False)
        self.pause.setEnabled(False)
        self.resume.setEnabled(False)
        self.add_log("Остановка запрошена. Ожидаю завершения текущей операции, до 180 секунд.")

    def open_result(self):
        if self.report and self.report.project:
            self.set_busy(True)
            self.pause.setEnabled(False)
            self.stop.setEnabled(False)
            folder=self.result_folder/("open-"+uuid.uuid4().hex[:8])
            self.opener=OpenProjectWorker(self.report.project,folder)
            self.opener.log.connect(self.add_log)
            self.opener.error.connect(lambda e:QMessageBox.warning(self,"Не удалось открыть проект",e))
            self.opener.finished.connect(self.worker_finished)
            self.opener.start()

    def open_report_file(self):
        if self.result_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result_folder/"report.html")))

    def closeEvent(self,event):
        if self.opener and self.opener.isRunning():
            self.close_when_finished=True
            event.ignore()
            return
        if self.loader and self.loader.isRunning():
            event.ignore()
            return
        if self.worker and self.worker.isRunning():
            self.close_when_finished=True
            self.stop_run()
            event.ignore()
            return
        event.accept()


def main():
    app=QApplication(sys.argv)
    initialize_fonts()
    app.setApplicationName("Packet Tracer Lab Agent")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window=MainWindow()
    window.show()
    if '--diagnose-gui' in sys.argv:
        # Render this application's own widgets for packaging diagnostics. No
        # Windows input automation, Cisco launch, network call or API key is used.
        output=Path(sys.argv[sys.argv.index('--diagnose-gui')+1]).resolve()
        output.mkdir(parents=True,exist_ok=True)
        from .course_switching import switching_plan
        window.show_plan(switching_plan(4)[0])
        app.processEvents()
        window.grab().save(str(output/'main-window.png'))
        results={}
        for lab in (2,3,4,5,6,7):
            dialog=CourseDialog(lab,window)
            dialog.show();app.processEvents()
            if lab==5:
                try:
                    dialog.request();results['5']=False
                except ValueError:
                    results['5']=True
            else:
                results[str(lab)]=dialog.request().lab==lab
            dialog.grab().save(str(output/f'lab-{lab}-dialog.png'))
            dialog.close()
        from . import __version__
        (output/'gui-diagnostics.json').write_text(json.dumps({'version':__version__,'title':window.windowTitle(),'dialogs':results},ensure_ascii=False),encoding='utf-8')
        window.close()
        return 0 if all(results.values()) else 1
    return app.exec()


if __name__=="__main__":
    raise SystemExit(main())

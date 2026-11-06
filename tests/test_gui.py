import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")

from PySide6.QtWidgets import QApplication
from ptagent.gui import MainWindow,AgentWorker


def test_gui_renders_and_disables_mutations_during_work():
    app=QApplication.instance() or QApplication([])
    window=MainWindow()
    window.show()
    app.processEvents()
    assert window.start.text()=="Начать выполнение"
    assert not window.open_project.isEnabled()
    window.set_busy(True)
    assert not window.load.isEnabled()
    assert not window.start.isEnabled()
    assert window.pause.isEnabled()
    window.set_busy(False)
    window.close()


def test_real_worker_emits_plan_and_writes_report_without_network(tmp_path):
    import json
    from pathlib import Path
    from PySide6.QtCore import QEventLoop,QTimer
    from ptagent.control import RunControl
    from ptagent.documents import load_assignment
    app=QApplication.instance() or QApplication([])
    assignment=load_assignment(Path(__file__).parents[1]/"examples"/"Простая_сеть.txt")
    worker=AgentWorker(assignment,tmp_path,False,True,RunControl())
    plans,results=[],[]
    loop=QEventLoop()
    worker.plan_ready.connect(plans.append)
    worker.result.connect(lambda report,folder:results.append(report))
    worker.finished.connect(loop.quit)
    timer=QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(10000)
    worker.start()
    loop.exec()
    assert worker.wait(1000),"Фоновый анализ не завершился"
    assert len(plans)==1 and len(plans[0].devices)==3
    assert results[0].status=="planned"
    assert json.loads((tmp_path/"report.json").read_text(encoding="utf-8"))["status"]=="planned"
    assert not list(tmp_path.glob("*.pkt"))

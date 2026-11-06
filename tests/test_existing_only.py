import asyncio
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from ptagent.desktop import DesktopError, WindowsDesktop


def test_closed_cisco_never_starts_process(monkeypatch):
    import subprocess
    monkeypatch.setattr(psutil,"process_iter",lambda attrs: [])
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**k:pytest.fail("Нельзя запускать Cisco"))
    with pytest.raises(DesktopError,match="закрыт"):
        WindowsDesktop(Path("C:/Cisco/PacketTracer.exe")).require_running()


def test_attach_to_matching_existing_process(monkeypatch):
    exe=Path("C:/Cisco/PacketTracer.exe")
    monkeypatch.setattr(psutil,"process_iter",lambda attrs:[SimpleNamespace(pid=123,info={"exe":str(exe)})])
    desktop=WindowsDesktop(exe)
    assert desktop.require_running()==123
    monkeypatch.setattr(psutil,"process_iter",lambda attrs:[])
    with pytest.raises(DesktopError):
        desktop.require_running()
    assert desktop.pid is None


def test_worker_rejects_closed_cisco_before_ai_and_bridge(monkeypatch,tmp_path):
    from ptagent import gui
    from ptagent.control import RunControl
    from ptagent.documents import Assignment
    monkeypatch.setattr(gui,"inspect_environment",lambda:{})
    def closed():
        raise DesktopError("Cisco закрыт")
    monkeypatch.setattr(gui,"require_open_cisco",closed)
    monkeypatch.setattr(gui,"analyze",lambda *a:pytest.fail("Не нужно тратить API при закрытом Cisco"))
    monkeypatch.setattr(gui,"connect_bridge",lambda *a:pytest.fail("Мост не должен запускаться"))
    worker=gui.AgentWorker(Assignment("test"),tmp_path,True,False,RunControl())
    with pytest.raises(DesktopError):
        asyncio.run(worker.execute())


def test_open_result_rejects_closed_cisco_without_shell_or_bridge(monkeypatch,tmp_path):
    from ptagent import gui
    def closed():
        raise DesktopError("Cisco закрыт")
    monkeypatch.setattr(gui,"require_open_cisco",closed)
    monkeypatch.setattr(gui,"connect_bridge",lambda *a:pytest.fail("Мост не должен запускаться"))
    monkeypatch.setattr(gui.QDesktopServices,"openUrl",lambda *a:pytest.fail("Нельзя открывать .pkt через Windows"))
    with pytest.raises(DesktopError):
        asyncio.run(gui.OpenProjectWorker(tmp_path/"lab.pkt",tmp_path).execute())


@pytest.mark.parametrize("save_exists",[False,True])
def test_open_result_uses_running_bridge_and_requires_backup(monkeypatch,tmp_path,save_exists):
    from contextlib import asynccontextmanager
    from ptagent import gui
    project=tmp_path/"lab.pkt"
    project.write_bytes(b"existing native project fixture")
    folder=tmp_path/"open"
    calls=[]
    monkeypatch.setattr(gui,"require_open_cisco",lambda:SimpleNamespace(require_running=lambda:123))
    class Bridge:
        async def call(self,name,**args):
            calls.append(name)
            if name=="pt_save_project" and save_exists:
                (Path(args["directory"])/args["filename"]).write_bytes(b"backup fixture")
            return "OK"
    @asynccontextmanager
    async def connect(*args):
        yield Bridge()
    monkeypatch.setattr(gui,"connect_bridge",connect)
    worker=gui.OpenProjectWorker(project,folder)
    if save_exists:
        asyncio.run(worker.execute())
        assert calls==["pt_bridge_status","pt_save_project","pt_open_project"]
    else:
        with pytest.raises(RuntimeError,match="резервная копия"):
            asyncio.run(worker.execute())
        assert "pt_open_project" not in calls

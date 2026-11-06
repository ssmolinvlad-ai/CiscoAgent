import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ptagent.bridge import BridgeError, ensure_reply, parse_topology, unpack_result
from ptagent.control import RunControl, Stopped
from ptagent.documents import Assignment, load_assignment
from ptagent.executor import Executor, cli_outputs, interface_commands, ping_status
from ptagent.models import Check, Device, Interface, LabPlan, RunReport
from ptagent.planner import APIPlanner, NeedsAI, OfflinePlanner
from ptagent.reporting import write_report


EXAMPLE=Path(__file__).parent.parent/"examples"/"Простая_сеть.txt"


def simple_plan():
    return asyncio.run(OfflinePlanner().plan(load_assignment(EXAMPLE)))


def test_offline_example_has_exact_ports_and_addresses():
    plan=simple_plan()
    assert len(plan.devices)==3
    assert plan.links[0].b.port=="FastEthernet0/1"
    assert plan.devices[1].interfaces[0].address=="192.168.10.10/24"
    assert len(plan.checks)==2


@pytest.mark.parametrize("filename",["../a.pkt","C:\\a.pkt","CON.pkt","a.exe","test:bad.pkt"])
def test_reject_unsafe_output_filename(filename):
    data=simple_plan().model_dump()
    data["filename"]=filename
    with pytest.raises(ValidationError):
        LabPlan.model_validate(data)


def test_duplicate_ip_is_rejected():
    data=simple_plan().model_dump()
    data["devices"][2]["interfaces"][0]["address"]="192.168.10.10/24"
    with pytest.raises(ValidationError,match="Дублирующийся IP"):
        LabPlan.model_validate(data)


def test_same_port_cannot_have_two_links():
    data=simple_plan().model_dump()
    data["links"][1]["b"]["port"]="FastEthernet0/1"
    with pytest.raises(ValidationError,match="Порт занят"):
        LabPlan.model_validate(data)


def test_invalid_gateway_is_rejected():
    data=simple_plan().model_dump()
    data["devices"][1]["gateway"]="10.0.0.1"
    with pytest.raises(ValidationError,match="вне подсети"):
        LabPlan.model_validate(data)


def test_unknown_requirement_never_silently_disappears():
    text=EXAMPLE.read_text(encoding="utf-8")+"\nВключить NAT overload на R1"
    with pytest.raises(NeedsAI):
        asyncio.run(OfflinePlanner().plan(Assignment(text)))


def test_offline_ios_mutations_are_revalidated():
    text=EXAMPLE.read_text(encoding="utf-8")+"\nios SW1:\nerase startup-config\nend-ios"
    with pytest.raises(ValidationError,match="Команда"):
        asyncio.run(OfflinePlanner().plan(Assignment(text)))


def test_every_requirement_needs_check_coverage():
    data=simple_plan().model_dump()
    data["requirements"]=["Связность", "VLAN 10"]
    data["checks"][0]["requirement_ids"]=[0]
    with pytest.raises(ValidationError,match="каждого требования"):
        LabPlan.model_validate(data)


def test_image_needs_ai():
    with pytest.raises(NeedsAI):
        asyncio.run(OfflinePlanner().plan(Assignment("diagram",[b"image"])))


def test_missing_ai_key_is_actionable(monkeypatch):
    monkeypatch.delenv("LAB_AI_KEY",raising=False)
    with pytest.raises(NeedsAI,match="LAB_AI_KEY"):
        asyncio.run(APIPlanner().plan(Assignment("task")))


def test_readonly_checks_cannot_run_config_commands():
    data=simple_plan().model_dump()
    data["checks"][0].update(kind="cli_contains",command="configure terminal",contains="x")
    with pytest.raises(ValidationError):
        LabPlan.model_validate(data)


@pytest.mark.parametrize("text",["No answer from PT (timeout)","Not saved: occupied","ERROR: unsupported","PT error: foo",'{"ok":false,"error":"missing"}'])
def test_plain_text_bridge_errors_are_failures(text):
    with pytest.raises(BridgeError):
        ensure_reply(text)


def test_mcp_error_flag_cannot_be_ignored():
    result=SimpleNamespace(isError=True,content=[SimpleNamespace(type="text",text="failed")])
    with pytest.raises(BridgeError):
        unpack_result(result)


def test_timeout_does_not_prove_denied_traffic():
    assert ping_status("No answer (timeout)",False)=="unverified"
    assert ping_status("NO CONNECTIVITY\n0/4",False)=="passed"
    assert ping_status("PARTIAL CONNECTIVITY (packet loss)",True)=="failed"


def test_topology_parser_checks_complete_inventory():
    text="DEVICES:2|LINKS:1\n  PC1 [PC-PT] (FastEthernet0=10.0.0.1/255.255.255.0)\n  SW1 [2960-24TT]\n  Links:\n  PC1:FastEthernet0  <--[straight]-->  SW1:FastEthernet0/1"
    data=parse_topology(text)
    assert len(data["devices"])==2
    assert data["links"][0]["a"]==("PC1","FastEthernet0")
    with pytest.raises(BridgeError):
        parse_topology(text.replace("DEVICES:2","DEVICES:3"))


def test_cli_transcript_does_not_match_echoed_command():
    text="R1 (CLI) — 2 command(s): 2 ok\n\n> enable    [ok]\nRouter#show ip route    [ok]\nCodes: C - connected\nC 10.0.0.0/24 is directly connected\nFinal prompt: Router#  (mode: priv)"
    output=cli_outputs(text)
    assert "show ip route" not in output
    assert "10.0.0.0" in output
    with pytest.raises(BridgeError):
        cli_outputs(text.replace("show ip route    [ok]","show ip route    [ERROR]"))


def test_native_topology_allows_auto_created_power_object():
    text="DEVICES:2|LINKS:0\n  SW1 [2960-24TT] (Vlan1,FastEthernet0/1)\n  Power Distribution Device0 [Power Distribution Device]\n"
    assert list(parse_topology(text)["devices"])==["SW1"]
    with pytest.raises(BridgeError):
        parse_topology(text.replace("DEVICES:2","DEVICES:3"))
    # Other multiword device names and models remain part of the inventory.
    other=text.replace("Power Distribution Device0 [Power Distribution Device]","User Server [Server-PT]")
    assert "User Server" in parse_topology(other)["devices"]


def test_compiler_respects_observed_interface_names():
    device=Device(name="R1",model="ISR4331",kind="router",interfaces=[Interface(name="GigabitEthernet0/0/0",address="10.2.3.1/24")])
    commands=interface_commands(device)
    assert "interface GigabitEthernet0/0/0" in commands
    assert "ip address 10.2.3.1 255.255.255.0" in commands


def test_pause_and_stop_are_cooperative():
    async def run():
        control=RunControl()
        control.pause()
        pending=asyncio.create_task(control.checkpoint())
        await asyncio.sleep(.02)
        assert not pending.done()
        control.stop()
        with pytest.raises(Stopped):
            await pending
    asyncio.run(run())


def test_report_escapes_html_and_redacts_credentials(tmp_path):
    report=RunReport(title="<script>alert(1)</script>",started_at="now",status="failed",errors=["password my-secret-value"])
    write_report(tmp_path,report)
    data=json.loads((tmp_path/"report.json").read_text(encoding="utf-8"))
    assert data["status"]=="failed"
    page=(tmp_path/"report.html").read_text(encoding="utf-8")
    assert "<script>" not in page
    assert "my-secret-value" not in page


def test_docx_keeps_table_values_and_paragraph_order(tmp_path):
    from docx import Document
    doc=Document()
    doc.add_paragraph("Before")
    table=doc.add_table(rows=1,cols=2)
    table.cell(0,0).text="PC1"
    table.cell(0,1).text="192.168.1.1/24"
    doc.add_paragraph("After")
    path=tmp_path/"lab.docx"
    doc.save(path)
    assignment=load_assignment(path)
    assert assignment.text.index("Before") < assignment.text.index("PC1") < assignment.text.index("After")
    assert assignment.text.count("Before")==1


def test_pdf_preserves_text_and_vector_diagram(tmp_path):
    import pymupdf
    path=tmp_path/"lab.pdf"
    with pymupdf.open() as pdf:
        page=pdf.new_page()
        page.insert_text((30,30),"PC1: 192.168.1.1/24")
        page.draw_line((30,60),(200,100))
        pdf.save(path)
    assignment=load_assignment(path)
    assert "PC1" in assignment.text
    assert assignment.images[0].startswith(b"\x89PNG")


class FakeBridge:
    """A simulator double. Its mock .pkt marker is NOT a Cisco project."""
    def __init__(self,bad_port=False,ping="CONNECTIVITY OK",save=True,wrong_ip=False):
        self.tools=dict.fromkeys(["pt_bridge_status","pt_query_topology","pt_get_device_details","pt_add_device","pt_add_link","pt_save_project","pt_read_device_panel","pt_host_ip_config","pt_verify_connectivity","pt_cli","pt_send_raw"])
        self.devices={}
        self.links=[]
        self.calls=[]
        self.bad_port,self.ping,self.save,self.wrong_ip=bad_port,ping,save,wrong_ip

    def validate(self,name,args):
        if name not in self.tools:
            raise BridgeError("missing tool")

    async def call(self,tool_name,**args):
        name=tool_name
        self.calls.append((name,args))
        if name=="pt_bridge_status":
            return "CONNECTED over file-bridge"
        if name=="pt_query_topology":
            lines=[f"DEVICES:{len(self.devices)}|LINKS:{len(self.links)}"]
            lines += [f"  {n} [{d['model']}]" for n,d in self.devices.items()]
            lines += [f"  {a['device1']}:{a['port1']} <--[{a['cable_type']}]--> {a['device2']}:{a['port2']}" for a in self.links]
            return "\n".join(lines)
        if name=="pt_get_device_details":
            ports=[] if self.bad_port else ["FastEthernet0","FastEthernet0/1","FastEthernet0/2"]
            return json.dumps({"ports":[{"name":p} for p in ports]})
        if name=="pt_add_device":
            self.devices[args["name"]]={"model":args["model"],"ports":[]}
            return "Device created"
        if name=="pt_send_raw":
            self.devices.clear()
            self.links.clear()
            return '{"ok":true}'
        if name=="pt_add_link":
            self.links.append(args)
            return "Link created"
        if name=="pt_host_ip_config":
            d=self.devices[args["device"]]
            d["ports"]=[{"name":args.get("interface","FastEthernet0"),"ip":"10.1.1.99" if self.wrong_ip else args.get("ip"),"mask":args.get("mask")}]
            return json.dumps({"ok":True})
        if name=="pt_read_device_panel":
            return json.dumps(self.devices[args["device"]])
        if name=="pt_verify_connectivity":
            return self.ping
        if name=="pt_cli":
            return json.dumps({"results":[{"command":c,"status":"ok","output":""} for c in args["commands"]]})
        if name=="pt_save_project":
            if self.save:
                (Path(args["directory"])/args["filename"]).write_text("MOCK ONLY - not Cisco",encoding="utf-8")
            return "Project saved"
        raise AssertionError(name)


def test_executor_full_workflow_against_simulator_double(tmp_path):
    bridge=FakeBridge()
    report=asyncio.run(Executor(bridge,tmp_path,RunControl()).run(simple_plan(),{}))
    assert report.status=="completed", report.errors
    assert report.project
    assert all(c.status=="passed" for c in report.checks)
    calls=[n for n,a in bridge.calls]
    assert calls.index("pt_save_project") < calls.index("pt_send_raw") < calls.index("pt_add_device") < calls.index("pt_add_link") < calls.index("pt_host_ip_config") < calls.index("pt_verify_connectivity")
    assert calls[-1]=="pt_save_project"


def test_unknown_model_port_blocks_before_any_mutation(tmp_path):
    bridge=FakeBridge(bad_port=True)
    report=asyncio.run(Executor(bridge,tmp_path,RunControl()).run(simple_plan(),{}))
    assert report.status=="failed"
    assert not bridge.devices


def test_false_save_acknowledgement_never_returns_project(tmp_path):
    bridge=FakeBridge(save=False)
    report=asyncio.run(Executor(bridge,tmp_path,RunControl()).run(simple_plan(),{}))
    assert report.status=="failed"
    assert report.project is None
    assert not any(name in ("pt_send_raw","pt_add_device") for name,args in bridge.calls)


def test_existing_topology_is_backed_up_before_native_reset(tmp_path):
    bridge=FakeBridge()
    bridge.devices["ExistingPC"]={"model":"PC-PT","ports":[]}
    report=asyncio.run(Executor(bridge,tmp_path,RunControl()).run(simple_plan(),{}))
    assert report.status=="completed"
    assert (tmp_path/"before-agent.pkt").is_file()
    assert "ExistingPC" not in bridge.devices
    names=[name for name,args in bridge.calls]
    assert names.index("pt_save_project") < names.index("pt_send_raw") < names.index("pt_add_device")


def test_packet_loss_is_preserved_and_retries_are_bounded(tmp_path):
    bridge=FakeBridge(ping="PARTIAL CONNECTIVITY (packet loss)")
    report=asyncio.run(Executor(bridge,tmp_path,RunControl()).run(simple_plan(),{}))
    assert report.status=="failed"
    assert report.project
    assert sum(n=="pt_verify_connectivity" for n,a in bridge.calls)==8


def test_accepted_config_but_wrong_live_ip_fails(tmp_path):
    bridge=FakeBridge(wrong_ip=True)
    report=asyncio.run(Executor(bridge,tmp_path,RunControl()).run(simple_plan(),{}))
    assert report.status=="failed"
    assert any(c.id.startswith("@ip") and c.status=="failed" for c in report.checks)


def test_stopped_agent_does_not_mutate(tmp_path):
    bridge=FakeBridge()
    control=RunControl()
    control.stop()
    report=asyncio.run(Executor(bridge,tmp_path,control).run(simple_plan(),{}))
    assert report.status=="stopped"
    assert bridge.calls==[]

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .bridge import BridgeError, json_reply, parse_topology
from .control import RunControl, Stopped
from .models import Check, CheckResult, Device, LabPlan, RunReport, Stage
from .reporting import redact, write_report


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def interface_commands(device:Device) -> list[str]:
    commands = ["enable","configure terminal"]
    for vlan,name in device.vlans.items():
        commands += [f"vlan {vlan}",f"name {name}","exit"]
    for interface in device.interfaces:
        commands += [f"interface {interface.name}"]
        if interface.vlan:
            commands += ["switchport mode access",f"switchport access vlan {interface.vlan}"]
        if interface.address:
            addr = ipaddress.IPv4Interface(interface.address)
            commands += [f"ip address {addr.ip} {addr.netmask}"]
        commands += ["no shutdown","exit"]
    commands += ["end"]
    return commands


def cli_outputs(text:str) -> str:
    if not text.lstrip().startswith(("{","```")):
        # Upstream 0.11 emits a transcript, not JSON.
        header=re.search(r"^.+ \((?:CLI|Command Prompt)\) — (\d+) command\(s\):",text)
        marks=re.findall(r"(?m)^.*\s+\[(ok|ERROR|UNKNOWN COMMAND|WAITING FOR ANSWER|TIMEOUT)\]\s*$",text)
        if not header or len(marks)!=int(header[1]) or any(m!="ok" for m in marks) or "Interrupted:" in text:
            raise BridgeError("CLI не подтвердил команды: "+text[:700])
        return "\n".join(line for line in text.splitlines()[1:] if not re.search(r"\s+\[ok\]\s*$",line) and not line.startswith("Final prompt:"))
    data = json_reply(text)
    # Current upstream returns a results array: don't search echoed input arguments.
    results = data.get("results")
    if not isinstance(results,list) or not results:
        raise BridgeError("Неизвестный формат ответа CLI")
    output=[]
    for row in results:
        if row.get("status") != "ok":
            raise BridgeError("CLI не подтвердил команду: " + json.dumps(row,ensure_ascii=False)[:600])
        value = str(row.get("output", ""))
        command = str(row.get("command", ""))
        # Exclude echoed command (including router prompt) from evidence matching.
        output += [line for line in value.splitlines() if not (command and line.strip().endswith(command))]
    return "\n".join(output)


def ping_status(text:str, expected:bool) -> str:
    if "PARTIAL CONNECTIVITY" in text:
        return "failed"
    if "CONNECTIVITY OK" in text:
        return "passed" if expected else "failed"
    if "NO CONNECTIVITY" in text:
        # A measured 0% response is evidence; a timeout/error is never an ACL success.
        return "failed" if expected else "passed"
    return "unverified"


READ_TOOLS = {"pt_read_routes","pt_read_ospf","pt_read_arp","pt_read_cdp","pt_read_mac_table", "pt_read_stp",
              "pt_read_acl","pt_read_nat","pt_read_vlans","pt_read_dhcp_leases","pt_read_device_panel","pt_read_qos"}


class Executor:
    def __init__(self,bridge,folder:Path,control:RunControl,on_stage=lambda *args:None,on_log=lambda *args:None):
        self.bridge,self.folder,self.control = bridge,folder,control
        self.on_stage,self.on_log = on_stage,on_log
        self.folder.mkdir(parents=True,exist_ok=True)
        self.sequence=0

    async def call(self,tool_name,**args):
        await self.control.checkpoint()
        result = await self.bridge.call(tool_name,**args)
        self.sequence+=1
        # Real raw evidence with credentials removed; never log API requests/headers.
        (self.folder / f"evidence-{self.sequence:04d}-{tool_name}.txt").write_text(redact(result),encoding="utf-8")
        return result

    def stage(self,value,progress):
        self.on_stage(str(value),progress)
        self.on_log(str(value))

    async def snapshot(self):
        return parse_topology(await self.call("pt_query_topology"))

    async def configure(self,device:Device,repair=False):
        if device.kind in ("pc","server"):
            if device.dhcp:
                await self.call("pt_host_ip_config",device=device.name,mode="dhcp")
            for interface in device.interfaces:
                if interface.address:
                    addr=ipaddress.IPv4Interface(interface.address)
                    await self.call("pt_host_ip_config",device=device.name,interface=interface.name,mode="static",ip=str(addr.ip),mask=str(addr.netmask),gateway=device.gateway or "",dns=device.dns or "")
        elif device.kind in ("router","switch"):
            if device.interfaces or device.vlans:
                cli_outputs(await self.call("pt_cli",device=device.name,commands=interface_commands(device)))
            if device.ios and not repair:
                cli_outputs(await self.call("pt_cli",device=device.name,commands=["enable","configure terminal",*device.ios,"end"]))
            cli_outputs(await self.call("pt_cli",device=device.name,commands=["enable","write memory"]))

    async def preflight(self,plan):
        required={"pt_bridge_status","pt_query_topology","pt_get_device_details","pt_add_device","pt_add_link","pt_save_project","pt_read_device_panel","pt_send_raw"}
        if any(d.kind in ("router","switch") for d in plan.devices):
            required.add("pt_cli")
        if any(d.kind in ("pc","server") and (d.interfaces or d.dhcp) for d in plan.devices):
            required.add("pt_host_ip_config")
        for check in plan.checks:
            required.add({"ping":"pt_verify_connectivity","cli_contains":"pt_cli","host_contains":"pt_host_command","host_ip":"pt_read_device_panel","http_contains":"pt_web_browser","tool_contains":check.tool}[check.kind])
            if check.kind=="tool_contains":
                if check.tool not in READ_TOOLS:
                    raise BridgeError(f"Операция проверки {check.tool} не входит в список чтения")
                if any(k in check.arguments for k in ("device","switch","router")):
                    if not all(check.arguments[k]==check.device for k in ("device","switch","router") if k in check.arguments):
                        raise BridgeError("Устройство проверки расходится с её параметрами")
                self.bridge.validate(check.tool,check.arguments)
        for service in plan.services:
            required.add(service.tool)
            if "device" in service.arguments and service.arguments["device"]!=service.device:
                raise BridgeError("Устройство службы расходится с её параметрами")
            self.bridge.validate(service.tool,{**service.arguments,"device":service.device})
        missing=required-self.bridge.tools.keys()
        if missing:
            raise BridgeError("Отсутствуют операции моста: " + ", ".join(sorted(missing)))
        models={}
        for device in plan.devices:
            if device.model not in models:
                models[device.model]=json_reply(await self.call("pt_get_device_details",model_name=device.model))
            ports={p["name"] for p in models[device.model].get("ports",[])}
            wanted={i.name for i in device.interfaces}
            for link in plan.links:
                wanted.update(e.port for e in (link.a,link.b) if e.device==device.name)
            if wanted-ports:
                raise BridgeError(f"У {device.name} ({device.model}) отсутствуют порты: {', '.join(sorted(wanted-ports))}. Дополнительные модули пока не устанавливаются.")

    async def verify_device(self,device,attempt=1):
        data=json_reply(await self.call("pt_read_device_panel",device=device.name))
        ports={p.get("name"):p for p in data.get("ports",[]) if isinstance(p,dict)}
        results=[]
        for index,interface in enumerate(device.interfaces):
            if interface.address:
                addr=ipaddress.IPv4Interface(interface.address)
                observed=ports.get(interface.name,{})
                valid=observed.get("ip")==str(addr.ip) and observed.get("mask")==str(addr.netmask)
                results.append(CheckResult(id=f"@ip_{device.name}_{index}",description=f"Адрес и маска {device.name}:{interface.name} = {addr}",status="passed" if valid else "failed",evidence=json.dumps(observed,ensure_ascii=False),attempt=attempt))
        if device.gateway:
            results.append(CheckResult(id=f"@gateway_{device.name}",description=f"Шлюз {device.name} = {device.gateway}",status="passed" if str(data.get("gateway"))==device.gateway else "failed",evidence=str(data.get("gateway")),attempt=attempt))
        if device.dhcp:
            ips=[p.get("ip") for p in ports.values()]
            leased=data.get("dhcp_flag") is True and any(ip and ip!="0.0.0.0" and not str(ip).startswith("169.254.") for ip in ips)
            results.append(CheckResult(id=f"@dhcp_{device.name}",description=f"DHCP-клиент и адрес {device.name}",status="passed" if leased else "failed",evidence=json.dumps(data,ensure_ascii=False),attempt=attempt))
        return results

    async def check(self,check:Check,attempt=1):
        try:
            if check.kind=="ping":
                evidence=await self.call("pt_verify_connectivity",from_device=check.device,to_ip=check.target)
                status=ping_status(evidence,check.expected)
                # First ARP/STP convergence is not a repair and may be retried once.
                if status!="passed":
                    evidence=await self.call("pt_verify_connectivity",from_device=check.device,to_ip=check.target)
                    status=ping_status(evidence,check.expected)
            elif check.kind=="cli_contains":
                evidence=cli_outputs(await self.call("pt_cli",device=check.device,commands=["enable",check.command]))
                status="passed" if ((check.contains in evidence)==check.expected) else "failed"
            elif check.kind=="host_contains":
                evidence=cli_outputs(await self.call("pt_host_command",device=check.device,command=check.command))
                status="passed" if ((check.contains in evidence)==check.expected) else "failed"
            elif check.kind=="host_ip":
                evidence=await self.call("pt_read_device_panel",device=check.device)
                data=json_reply(evidence)
                addresses={str(p.get("ip")) for p in data.get("ports",[]) if isinstance(p,dict)}
                status="passed" if ((check.target in addresses)==check.expected) else "failed"
            elif check.kind=="http_contains":
                evidence=await self.call("pt_web_browser",device=check.device,url=check.target)
                data=json_reply(evidence)
                content=data.get("text",data.get("content"))
                if data.get("ok") is False or data.get("warning") or not isinstance(content,str):
                    status="unverified"
                else:
                    status="passed" if ((check.contains in content)==check.expected) else "failed"
            else:
                evidence=await self.call(check.tool,**check.arguments)
                if not json_reply(evidence):
                    raise BridgeError("Пустое наблюдение не подтверждает проверку")
                status="passed" if ((check.contains in evidence)==check.expected) else "failed"
            return CheckResult(id=check.id,description=check.description,status=status,evidence=evidence,attempt=attempt)
        except BridgeError as exc:
            return CheckResult(id=check.id,description=check.description,status="unverified",evidence=str(exc),attempt=attempt)

    async def run(self,plan:LabPlan,environment:dict,desktop=None) -> RunReport:
        report=RunReport(title=plan.title,started_at=now(),environment=environment,assumptions=plan.assumptions)
        (self.folder/"plan.json").write_text(plan.model_dump_json(indent=2),encoding="utf-8")
        try:
            if plan.unresolved:
                raise ValueError("Требуются уточнения: " + "; ".join(plan.unresolved))
            self.stage(Stage.BUILD,15)
            deadline=time.monotonic()+30
            while True:
                try:
                    status=await self.call("pt_bridge_status")
                    break
                except BridgeError as exc:
                    if "not connected" not in str(exc).lower() or time.monotonic()>=deadline:
                        raise
                    await self.control.checkpoint()
                    await asyncio.sleep(1)
            if not re.search(r"\bCONNECTED\b",status) or "NOT CONNECTED" in status:
                raise BridgeError("Расширение Cisco не подключено. Откройте «Подключение Cisco» в приложении. " + status[:500])
            await self.preflight(plan)
            current=await self.snapshot()
            self.on_log("Сохраняю резервную копию открытой лабораторной")
            backup=self.folder/"before-agent.pkt"
            await self.call("pt_save_project",filename=backup.name,directory=str(self.folder),overwrite=False)
            if not backup.is_file() or backup.stat().st_size==0:
                raise BridgeError("Резервная копия текущего проекта не подтверждена")
            # Cisco 9.0 installed IPC documentation: AppWindow.fileNew(bool),
            # false suppresses the save prompt. Use ONLY after a verified backup.
            await self.call("pt_send_raw",js_code="try { var made=ipc.appWindow().fileNew(false); reportResult(JSON.stringify({ok:made===true})); } catch(e) { reportResult('ERROR:'+e); }",wait_result=True)
            current=await self.snapshot()
            if current["devices"] or current["links"]:
                raise BridgeError("Новая рабочая область не подтверждена; изменения остановлены")
            for device in plan.devices:
                await self.call("pt_add_device",name=device.name,model=device.model,x=device.x,y=device.y)
                live=await self.snapshot()
                if device.name not in live["devices"]:
                    raise BridgeError(f"Создание {device.name} не подтверждено")
                actual_model=live["devices"][device.name]["model"]
                if actual_model != device.model:
                    raise BridgeError(f"Модель {device.name}: ожидалась {device.model}, Cisco вернул {actual_model}")
                report.checks.append(CheckResult(id=f"@device_{device.name}",description=f"Устройство {device.name}: {device.model}",status="passed",evidence=actual_model))
            self.stage(Stage.CONNECT,35)
            for link in plan.links:
                await self.call("pt_add_link",device1=link.a.device,port1=link.a.port,device2=link.b.device,port2=link.b.port,cable_type=link.cable)
            live=await self.snapshot()
            for link in plan.links:
                endpoints={(link.a.device,link.a.port),(link.b.device,link.b.port)}
                found=any({l["a"],l["b"]}==endpoints and (l["cable"]==link.cable or link.cable=="auto" and not l["cable"].startswith("unknown")) for l in live["links"])
                report.checks.append(CheckResult(id=f"@cable_{len(report.checks)+1}",description=f"{link.a.device}:{link.a.port} ↔ {link.b.device}:{link.b.port} ({link.cable})",status="passed" if found else "failed",evidence=json.dumps(live["links"],ensure_ascii=False)))
                if not found:
                    raise BridgeError("Кабель/порты отличаются от задания")
            self.stage(Stage.CONFIGURE,50)
            # Infrastructure and services before DHCP clients.
            for device in sorted(plan.devices,key=lambda d:d.kind in ("pc","server")):
                if not device.dhcp:
                    await self.configure(device)
            for service in plan.services:
                await self.call(service.tool,**{**service.arguments,"device":service.device})
            for device in plan.devices:
                if device.dhcp:
                    await self.configure(device)
            self.stage(Stage.VERIFY,70)
            for device in plan.devices:
                report.checks.extend(await self.verify_device(device))
            for check in plan.checks:
                result=await self.check(check)
                report.checks.append(result)
                self.on_log(f"{check.description}: {result.status}")
                write_report(self.folder,report)
            failures=[c for c in plan.checks if next(r for r in reversed(report.checks) if r.id==c.id).status!="passed"]
            baseline_failures=set()
            for device in plan.devices:
                if any(r.status!="passed" and r.id.startswith((f"@ip_{device.name}_",f"@gateway_{device.name}",f"@dhcp_{device.name}")) for r in report.checks):
                    baseline_failures.add(device.name)
            if failures or baseline_failures:
                self.stage(Stage.REPAIR,82)
                repair_names=baseline_failures | {c.repair_device for c in failures if c.repair_device}
                for name in sorted(repair_names):
                    device=next(d for d in plan.devices if d.name==name)
                    self.on_log(f"Диагностика {device.name} и повтор базовой настройки (один раз)")
                    await self.call("pt_read_device_panel",device=device.name)
                    await self.configure(device,repair=True)
                    report.checks.extend(await self.verify_device(device,attempt=2))
                for check in failures:
                    report.checks.append(await self.check(check,attempt=2))
            self.stage(Stage.SAVE,92)
            target=self.folder/plan.filename
            native_dir=self.folder/"native"
            native_dir.mkdir(exist_ok=True)
            native=native_dir/"agent-result.pkt"
            # Upstream sanitizes Cyrillic and spaces in names. Save a genuine native
            # file first, then copy its bytes under the exact requested filename.
            await self.call("pt_save_project",filename=native.name,directory=str(native_dir),overwrite=False)
            if not native.is_file() or native.stat().st_size==0:
                raise BridgeError("Cisco не создал непустой .pkt; сохранение не подтверждено")
            if target.exists():
                raise BridgeError("Итоговый файл уже существует; перезапись не выполнена")
            shutil.copy2(native,target)
            if target.stat().st_size!=native.stat().st_size:
                raise BridgeError("Копия итогового проекта повреждена")
            report.project=str(target.resolve())
            latest={r.id:r for r in report.checks}
            report.status="completed" if all(r.status=="passed" for r in latest.values()) else "failed"
            if report.status=="failed":
                report.errors.append("Проект сохранён, но часть требований не подтверждена; см. таблицу проверок")
            if "pt_screenshot" in self.bridge.tools:
                try:
                    await self.call("pt_screenshot",filename="topology",fmt="png",output_dir=str(self.folder))
                except BridgeError:
                    self.on_log("Снимок топологии недоступен; результаты сети сохранены отдельно")
            self.stage(Stage.DONE,100)
        except Stopped as exc:
            report.status="stopped"
            report.errors.append(str(exc)+". Изменения в Cisco остаются в рабочей области; автоматического отката нет.")
        except Exception as exc:
            report.status="failed"
            report.errors.append(str(exc))
            self.on_log("Ошибка: " + str(exc))
        finally:
            report.finished_at=now()
            write_report(self.folder,report)
        return report

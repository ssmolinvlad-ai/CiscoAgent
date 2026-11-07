"""Stateful experiments. A saved topology is not evidence that an experiment passed."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shutil
from pathlib import Path

from .bridge import BridgeError, json_reply, parse_topology
from .control import Stopped
from .coursework import CourseRequest, REQUIREMENTS, TITLES
from .executor import Executor, cli_outputs, now, ping_status
from .models import CheckResult, RunReport
from .reporting import redact, write_report


def host_addresses(topology: dict) -> dict[str, str]:
    hosts = {}
    for name, rec in topology['devices'].items():
        if rec['model'] in ('PC-PT', 'Laptop-PT', 'Server-PT'):
            addresses = re.findall(r'=([0-9.]+)/([0-9.]+)', rec['ports'])
            if addresses and addresses[0][0] != '0.0.0.0' and not ipaddress.IPv4Address(addresses[0][0]).is_link_local:
                hosts[name] = addresses[0][0]
    return hosts


def trace_frames(data: dict) -> list[dict]:
    value = data.get('trace', data.get('frames', []))
    if isinstance(value, dict):
        value = value.get('trace', [])
    return value if isinstance(value, list) else []


def trace_protocols(frames):
    return [f.get('traffic_type') for f in frames]


def echo_received(frames, source):
    return any(f.get('device') == source and f.get('accepted') and
               any('received an Echo Reply' in d.get('description', '') for d in f.get('decisions', []))
               for f in frames)


def graph_distances(topology, nodes):
    graph = {n: set() for n in nodes}
    for link in topology['links']:
        a, b = link['a'][0], link['b'][0]
        if a in graph and b in graph:
            graph[a].add(b); graph[b].add(a)
    result = {}
    for start in nodes:
        dist, queue = {start: 0}, [start]
        for node in queue:
            for neighbor in graph[node] - dist.keys():
                dist[neighbor] = dist[node] + 1; queue.append(neighbor)
        result[start] = dist
    return result


class CourseRunner(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = []
        self.restorations = []
        self.secrets = []
        self.report = None
        self.last_arp_packet = None

    def clean(self, text):
        text = redact(str(text))
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '[учебный пароль скрыт]')
        return text

    async def call(self, tool_name, **args):
        await self.control.checkpoint()
        value = await self.bridge.call(tool_name, **args)
        self.sequence += 1
        safe = self.clean(value)
        (self.folder / f'evidence-{self.sequence:04d}-{tool_name}.txt').write_text(safe, encoding='utf-8')
        return value

    def observe(self, title, data):
        value = json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
        self.observations.append({'title': title, 'evidence': self.clean(value)})

    def requirement(self, index, passed, evidence, *, unverified=False):
        check = self.report.checks[index]
        check.status = 'unverified' if unverified else ('passed' if passed else 'failed')
        check.evidence = self.clean(evidence)
        self.on_log(f'{check.description}: {check.status}')
        write_report(self.folder, self.report)

    async def cli(self, device, commands):
        return cli_outputs(await self.call('pt_cli', device=device, commands=commands))

    async def config(self, device, commands):
        return await self.cli(device, ['enable', 'configure terminal', *commands, 'end'])

    async def host(self, device, command, **args):
        text=await self.call('pt_host_command', device=device, command=command, **args)
        if args.get('inputs'):
            marks=re.findall(r'\[(ok|ERROR|UNKNOWN COMMAND|WAITING FOR ANSWER|TIMEOUT)\]',text)
            header=re.search(r'— (\d+) command\(s\):',text)
            if not header or len(marks)!=int(header[1]) or any(m not in ('ok','WAITING FOR ANSWER') for m in marks) or marks[-1]!='ok' or not re.search(r'Final prompt: C:\\>\s+\(mode: user\)',text):
                raise BridgeError('Интерактивный сеанс не завершён: '+self.clean(text))
            return text
        return cli_outputs(text)

    async def realtime(self):
        await self.call('pt_simulation_mode', on=False)

    async def settle(self, rounds=3):
        await self.realtime()
        for _ in range(rounds):
            await self.call('pt_fast_forward')
            await asyncio.sleep(0.15)

    async def ping(self, source, target, *, expected=True, attempts=2):
        evidence = ''
        for _ in range(attempts):
            evidence = await self.call('pt_verify_connectivity', from_device=source, to_ip=target)
            if ping_status(evidence, expected) == 'passed':
                return True, evidence
            # A timed-out tool raises; it is never accepted as a measured negative ping.
            if expected:
                await self.settle(1)
        return False, evidence

    async def capture(self, title):
        result = await self.call('pt_screenshot', filename=f'course-{len(self.observations):03d}', fmt='png', output_dir=str(self.folder))
        match = re.search(r'([A-Za-z]:[/\\][^\r\n]*?\.png)', result)
        if match and Path(match[1]).is_file():
            destination = self.folder / f'screenshot-{len(self.observations):03d}.png'
            shutil.copy2(match[1], destination)
            self.observations.append({'title': title, 'image': destination.name})
        else:
            self.observe(title, result)

    async def trace(self, source, destination, *, reset=True, steps=30):
        self.last_arp_packet=None
        await self.call('pt_simulation_mode', on=True)
        if reset:
            await self.call('pt_simulation_step', action='reset', times=1)
        await self.call('pt_send_pdu', source=source, destination=destination, simulation=True, steps=0)
        await self.call('pt_simulation_step', action='forward', times=steps)
        data = json_reply(await self.call('pt_read_packet_trace', limit=200, include_decisions=True))
        frames = trace_frames(data)
        if data.get('total_in_event_list', data.get('total', 0)) > 200:
            raise BridgeError('Более 200 событий: трасса усечена, вывод об эксперименте запрещён')
        self.observe(f'Трасса {source} → {destination}', data)
        arp_index=next((f.get('index') for f in frames if f.get('traffic_type')=='ARP' and f.get('out_port')),None)
        if arp_index is not None:
            script=("(function(){try{var f=ipc.simulation().getFrameInstanceAt("+str(int(arp_index))+
                    ");var p=f.getOutFrame();reportResult(JSON.stringify({frame_index:"+str(int(arp_index))+
                    ",pdu:p}));}catch(e){reportResult(JSON.stringify({unavailable:String(e)}));}})();")
            packet=json_reply(await self.call('pt_send_raw',js_code=script,wait_result=True))
            self.observe('Поля одного настоящего ARP пакета',packet)
            self.last_arp_packet=packet.get('pdu',{}).get('payload')
        return frames

    async def save_native(self, filename):
        await self.realtime()
        await self.call('pt_save_project', filename=filename, directory=str(self.folder), overwrite=False)
        path = self.folder / filename
        if not path.is_file() or path.stat().st_size < 100:
            raise BridgeError('Не подтверждён настоящий файл .pkt')
        return path

    async def run_course(self, request: CourseRequest, environment: dict, desktop=None):
        self.secrets = [request.console_password, request.enable_password]
        self.report = RunReport(title=f'Лабораторная №{request.lab}. {TITLES[request.lab]}', started_at=now(), environment=environment,
            checks=[CheckResult(id=f'requirement_{i}', description=s, status='unverified', evidence='Этап ещё не выполнен')
                    for i, s in enumerate(REQUIREMENTS[request.lab])])
        report = self.report
        if request.validation_only:
            report.assumptions.append('Проверка исполнителя на искусственном стенде. Это не готовая работа по исходной топологии студента из №5.')
        try:
            if desktop:
                desktop.require_running()
            self.stage('Подключение и резервное сохранение', 5)
            status = await self.call('pt_bridge_status')
            if not re.search(r'\bCONNECTED\b', status) or 'NOT CONNECTED' in status:
                raise BridgeError('Откройте модуль MCP Control Center в работающем Cisco')
            await self.save_native('before-course.pkt')
            if request.previous_project:
                previous = Path(request.previous_project).resolve()
                if not previous.is_file():
                    raise ValueError('Исходный .pkt не найден: ' + str(previous))
                await self.call('pt_open_project', path=str(previous))
            elif request.lab not in (3,5) and not request.use_current:
                raise ValueError('Выберите исходный .pkt предыдущей лабораторной')
            self.observe('План эксперимента', {'lab': request.lab, 'variant': request.variant,
                'requirements': REQUIREMENTS[request.lab], 'previous_project': request.previous_project or 'Уже открытая сеть'})
            await self.call('pt_list_devices')
            self.stage('Выполнение последовательности экспериментов', 15)
            if request.lab == 2:
                await self.arp(request)
            elif request.lab == 3:
                from .course_switching import switching
                await switching(self, request)
            elif request.lab == 4:
                from .course_switching import resilience
                await resilience(self, request)
            elif request.lab == 5:
                from .course_static import static_routing
                await static_routing(self,request)
            else:
                from .course_routing import routing
                await routing(self, request)
        except Stopped as exc:
            report.status = 'stopped'; report.errors.append(str(exc))
        except Exception as exc:
            report.status = 'failed'; report.errors.append(self.clean(exc))
            self.on_log('Ошибка эксперимента: ' + self.clean(exc))
        finally:
            # Restore temporary outages even if Stop was pressed. No checkpoint here.
            for name, args in reversed(self.restorations):
                try:
                    await self.bridge.call(name, **args)
                except Exception as exc:
                    report.errors.append('Не подтверждено восстановление: ' + self.clean(exc))
            try:
                await self.bridge.call('pt_simulation_mode', on=False)
                value = await self.bridge.call('pt_save_project', filename=f'lab-{request.lab}-result.pkt', directory=str(self.folder), overwrite=False)
                path = self.folder / f'lab-{request.lab}-result.pkt'
                if not path.is_file() or path.stat().st_size < 100:
                    raise BridgeError('Файл результата не создан: ' + str(value))
                report.project = str(path.resolve())
            except Exception as exc:
                report.errors.append('Сохранение: ' + self.clean(exc))
            if report.status == 'running':
                report.status = 'completed' if all(c.status == 'passed' for c in report.checks) and not report.errors else 'failed'
            report.finished_at = now()
            (self.folder / 'observations.json').write_text(json.dumps(self.observations, ensure_ascii=False, indent=2), encoding='utf-8')
            write_report(self.folder, report)
            from .course_report import write_course_report
            write_course_report(self.folder, report, self.observations)
            self.stage('Завершено' if report.status == 'completed' else 'Есть неподтверждённые требования — см. отчёт', 100)
        return report

    async def arp(self, request):
        topology = await self.snapshot()
        expected_hosts=[n for n,d in topology['devices'].items() if d['model'] in ('PC-PT','Laptop-PT','Server-PT')]
        await self.settle(3)
        dhcp_servers=[]
        for n in expected_hosts:
            if topology['devices'][n]['model']=='Server-PT':
                server=json_reply(await self.call('pt_read_device_panel',device=n))
                if server.get('services',{}).get('DhcpServerMain'):
                    dhcp_servers.append(n)
        for host in expected_hosts:
            panel=json_reply(await self.call('pt_read_device_panel',device=host))
            invalid=host not in host_addresses(topology)
            if invalid and not panel.get('dhcp_flag') and panel.get('model')!='Server-PT' and dhcp_servers:
                reachable=graph_distances(topology,list(topology['devices']))[host]
                if any(server in reachable for server in dhcp_servers):
                    await self.call('pt_host_ip_config',device=host,interface='FastEthernet0',mode='dhcp')
                    self.observe('Исправление адреса '+host,'Хост без адреса подключён к сегменту с работающим DHCP-сервером; включён DHCP-клиент.')
                    panel['dhcp_flag']=True
            if panel.get('dhcp_flag'):
                await self.host(host,'ipconfig /renew')
        await self.settle(2)
        topology=await self.snapshot()
        hosts = host_addresses(topology)
        missing=set(expected_hosts)-hosts.keys()
        if len(hosts) < 2 or missing:
            raise ValueError('Нужна сеть №1 с назначенными IPv4-адресами всех ПК. Без адреса: '+', '.join(sorted(missing)))
        for host in hosts:
            panel = json_reply(await self.call('pt_read_device_panel', device=host))
            self.observe('Адреса ' + host, panel)
        self.requirement(0, True, f'Продолжена исходная сеть; адреса прочитаны: {hosts}')
        a, b = list(hosts)[:2]
        await self.settle()
        await self.call('pt_simulation_mode', on=True)
        await self.call('pt_simulation_step', action='reset', times=1)
        for host in hosts:
            await self.host(host, 'arp -d')
        empty = json_reply(await self.call('pt_read_arp', device=a))
        self.observe('ARP до первого обмена', empty)
        self.requirement(1, all(not d.get('entries') for d in empty.get('devices', [])) and bool(empty.get('devices')), str(empty))
        cold = await self.trace(a, b, reset=False, steps=40)
        protocols = trace_protocols(cold)
        decisions = [d.get('description','') for f in cold for d in f.get('decisions',[])]
        self.requirement(2, 'ARP' in protocols and 'ICMP' in protocols and echo_received(cold, a)
                         and isinstance(self.last_arp_packet,dict) and self.last_arp_packet.get('pduType')=='ArpPacket'
                         and any('ARP frame is a request' in d for d in decisions)
                         and any('ARP frame is a reply' in d for d in decisions),
                         f'Протоколы: {protocols}; решения OSI записаны в observations.json')
        for host in hosts:
            self.observe('ARP после первого обмена: ' + host, await self.call('pt_read_arp', device=host))
        self.requirement(3, True, 'Сняты фактические ARP-таблицы каждого ПК после первого обмена')
        await self.realtime()
        all_ok = True
        for source in hosts:
            for destination, ip in hosts.items():
                if source != destination:
                    ok, evidence = await self.ping(source, ip)
                    all_ok &= ok; self.observe(f'Ping {source} → {destination}', evidence)
        self.requirement(4, all_ok, 'Выполнены направленные ping для каждой пары ПК')
        for host in hosts:
            self.observe('ARP после всех ping: ' + host, await self.call('pt_read_arp', device=host))
        # Reset the event list BEFORE inspecting warm-cache state. ResetSimulation may restore state.
        await self.call('pt_simulation_mode', on=True)
        await self.call('pt_simulation_step', action='reset', times=1)
        warm_cache = json_reply(await self.call('pt_read_arp', device=a))
        has_destination = any(e.get('ip') == hosts[b] for d in warm_cache.get('devices',[]) for e in d.get('entries',[]))
        warm = await self.trace(a, b, reset=False, steps=25)
        needed_arp = any('not in the ARP table' in d.get('description','') for f in warm for d in f.get('decisions',[]))
        self.requirement(5, has_destination and echo_received(warm,a) and not needed_arp,
                         f'До повтора адрес в кэше: {has_destination}; требуется ARP: {needed_arp}; холодная/тёплая трассы сохранены')
        await self.realtime()
        empty_all = True
        for host in hosts:
            await self.host(host, 'arp -d')
            table = json_reply(await self.call('pt_read_arp', device=host))
            empty_all &= bool(table.get('devices')) and all(not d.get('entries') for d in table.get('devices',[]))
        self.requirement(6, empty_all, 'После arp -d таблицы прочитаны повторно')
        if request.physical_windows:
            from .windows_arp import run_windows_arp
            try:
                measured=await asyncio.to_thread(run_windows_arp,self.folder,request.physical_interface)
                self.observe('Опыт ARP на физическом компьютере',measured)
                learned=measured.get('learned',[])
                wrong=measured.get('wrong_mac',[])
                valid=bool(learned) and bool(wrong) and measured.get('restored') is True and measured.get('ping_before_exit')==0 and measured.get('ping_after_exit')!=0
                self.requirement(7,valid,'Данные Windows-опыта и восстановление: '+str(measured))
            except Exception as exc:
                self.requirement(7,False,str(exc),unverified=True)
        else:
            self.requirement(7, False, 'Физическая Windows-часть не разрешена для этого запуска. Она временно меняет ARP-запись настоящего шлюза.', unverified=True)
        await self.capture('Итоговая сеть ARP')

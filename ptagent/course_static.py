"""Lab 5: staged static routing, using an explicitly supplied variant plan only."""
import hashlib
import ipaddress
import itertools
from collections import deque
from pathlib import Path

from .bridge import BridgeError
from .models import LabPlan,Check
from .executor import Executor
from .course_routing import read_routes,route_metric,traceroutes


def static_routes(plan):
    """Derive next hops for every remote LAN/WAN; never invent topology or ports."""
    if plan.unresolved or plan.services:
        raise ValueError('Схема №5 должна быть однозначной и не содержать посторонних служб')
    routers={d.name:d for d in plan.devices if d.kind=='router'}
    hosts={d.name:d for d in plan.devices if d.kind=='pc'}
    if len(routers)<3 or len(hosts)!=len(routers) or len(plan.devices)!=len(routers)+len(hosts):
        raise ValueError('№5: минимум три роутера, ровно один ПК на роутер, без посторонних устройств')
    addresses={(d.name,i.name):ipaddress.IPv4Interface(i.address) for d in plan.devices for i in d.interfaces if i.address}
    if any(a.network.prefixlen!=24 for a in addresses.values()):
        raise ValueError('В №5 LAN и WAN должны иметь маску /24')
    if any(d.ios or d.dhcp or d.vlans for d in plan.devices):
        raise ValueError('Исходный план №5 должен содержать адреса и кабели без IOS/маршрутизации/DHCP/VLAN')
    graph={name:[] for name in routers}; networks={}; attachments={}
    for link in plan.links:
        a,b=(link.a.device,link.a.port),(link.b.device,link.b.port)
        if a not in addresses or b not in addresses or addresses[a].network!=addresses[b].network:
            raise ValueError('Оба конца каждой линии должны иметь адреса одной /24')
        network=addresses[a].network
        if network in networks:raise ValueError('Подсети отдельных линий должны быть уникальны')
        owners={n for n,p in (a,b) if n in routers}
        networks[network]=owners
        if len(owners)==2:
            graph[a[0]].append((b[0],str(addresses[b].ip)))
            graph[b[0]].append((a[0],str(addresses[a].ip)))
        elif len(owners)==1:
            router=next(iter(owners));host=b[0] if a[0]==router else a[0]
            if host not in hosts or host in attachments or router in attachments.values():
                raise ValueError('Требуется ровно одно прямое подключение ПК к каждому роутеру')
            router_endpoint=a if a[0]==router else b
            if hosts[host].gateway!=str(addresses[router_endpoint].ip):
                raise ValueError('Шлюз ПК должен совпадать с адресом подключённого роутера')
            attachments[host]=router
        else:raise ValueError('В №5 недопустима линия между двумя ПК')
    if set(attachments)!=set(hosts) or set(attachments.values())!=set(routers):
        raise ValueError('Не каждый роутер имеет свой ПК')
    if set(addresses)!={(ep.device,ep.port) for link in plan.links for ep in (link.a,link.b)}:
        raise ValueError('Все адресованные интерфейсы должны быть подключены')
    result={}
    for source in routers:
        hops={source:None};queue=deque([source])
        while queue:
            node=queue.popleft()
            for neighbor,ip in sorted(graph[node]):
                if neighbor not in hops:
                    hops[neighbor]=ip if node==source else hops[node];queue.append(neighbor)
        if set(hops)!=set(routers):raise ValueError('Граф роутеров не связан')
        commands=[]
        for network,owners in sorted(networks.items()):
            if source in owners:continue
            # Select the nearest owner deterministically by a BFS from source.
            queue=deque([source]);seen={source};owner=None
            while queue and owner is None:
                node=queue.popleft()
                if node in owners:owner=node;break
                for neighbor,_ in sorted(graph[node]):
                    if neighbor not in seen:seen.add(neighbor);queue.append(neighbor)
            commands.append(f'ip route {network.network_address} {network.netmask} {hops[owner]}')
        result[source]=commands
    return result


async def static_routing(r,request):
    path=Path(request.topology_plan)
    plan=LabPlan.model_validate_json(path.read_text(encoding='utf-8-sig'))
    commands=static_routes(plan)
    r.observe('Источник точной схемы варианта',{'variant':request.variant,'path':str(path.resolve()),
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'plan':plan.model_dump()})
    # Initial connectivity tests must run before installing any static routes.
    baseline_plan=plan.model_copy(deep=True)
    first_host=next(d for d in plan.devices if d.kind=='pc')
    baseline_plan.checks=[Check(id='local_gateway',description='ПК достигает собственного шлюза до настройки маршрутов',kind='ping',device=first_host.name,target=first_host.gateway)]
    result=await Executor(r.bridge,r.folder/'static-build',r.control,r.on_stage,r.on_log).run(baseline_plan,{})
    if result.status!='completed':
        raise BridgeError('Исходная сеть №5 не подтверждена: '+'; '.join(result.errors))
    await r.settle(4)
    top=await r.snapshot()
    expected_devices={d.name for d in plan.devices}
    expected_links={frozenset(((l.a.device,l.a.port),(l.b.device,l.b.port))) for l in plan.links}
    actual_links={frozenset((tuple(l['a']),tuple(l['b']))) for l in top['links']}
    r.requirement(0,set(top['devices'])==expected_devices and actual_links==expected_links,'Нативные устройства и соединения сверены с переданным планом варианта')
    r.requirement(1,True,'Все LAN и WAN проверены: непересекающиеся уникальные /24, по одному ПК с правильным шлюзом на роутер')
    port_ok=True
    for router in commands:
        output=await r.cli(router,['show ip interface brief']);r.observe('Порты '+router,output)
        device=next(d for d in plan.devices if d.name==router)
        for interface in device.interfaces:
            address=str(ipaddress.IPv4Interface(interface.address).ip)
            port_ok &= any(interface.name in line and address in line and 'up' in line and line.split()[-2:]==['up','up'] for line in output.splitlines())
    r.requirement(2,port_ok,'Полные show ip interface brief каждого роутера сохранены в observations.json')
    hosts={d.name:str(ipaddress.IPv4Interface(d.interfaces[0].address).ip) for d in plan.devices if d.kind=='pc'}
    before=True
    for source,target in itertools.permutations(hosts,2):
        ok,text=await r.ping(source,hosts[target],expected=False);before &= ok
        r.observe(f'До маршрутов {source} → {target}',text)
    r.requirement(3,before,'Проверены все направленные межсетевые пары до установки маршрутов')
    for router,lines in commands.items():
        await r.config(router,lines);await r.cli(router,['write memory'])
    config_ok=True
    for router,lines in commands.items():
        output=await r.cli(router,['show running-config']);r.observe('Статическая конфигурация '+router,output)
        config_ok &= all(line in output for line in lines)
    r.requirement(4,config_ok,str(commands))
    table=await read_routes(r,'Фактические статические маршруты')
    tables_ok=True
    for router,lines in commands.items():
        output=await r.cli(router,['show ip route']);r.observe('show ip route '+router,output)
        for line in lines:
            parts=line.split();entry=route_metric(table,router,parts[2])
            tables_ok &= bool(entry and entry.get('protocol')=='S' and entry.get('next_hop')==parts[4])
    r.requirement(5,tables_ok,'Нативные таблицы маршрутизации содержат S и ожидаемые next-hop для всех удалённых LAN/WAN')
    after=True
    for source,target in itertools.permutations(hosts,2):
        ok,text=await r.ping(source,hosts[target]);after &= ok
        r.observe(f'После маршрутов {source} → {target}',text)
    r.requirement(6,after,'Все направленные межсетевые пары после настройки')
    r.requirement(7,await traceroutes(r,hosts,'Трассировка статической сети'),'Три фактические трассировки сохранены в observations.json')
    await r.capture('Статическая маршрутизация — готовая сеть')

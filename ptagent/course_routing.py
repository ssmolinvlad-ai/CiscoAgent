from __future__ import annotations

import ipaddress
import itertools
import re

from .bridge import BridgeError,json_reply
from .course_runner import graph_distances,host_addresses
from .course_switching import free_ports


ROUTER_MODELS={'1941','2901','2911','2811','1841','2620XM','2621XM','ISR4321','ISR4331','Router-PT'}


def router_nodes(top):
    return [n for n,d in top['devices'].items() if d['model'] in ROUTER_MODELS]


def interface_addresses(rec):
    return {port:str(ipaddress.IPv4Interface(f'{ip}/{mask}'))
            for port,ip,mask in re.findall(r'([A-Za-z]+\d+(?:/\d+)*)=([0-9.]+)/([0-9.]+)',rec['ports'])}


def unused_network(top,prefix,preferred):
    used=[ipaddress.IPv4Interface(value).network for rec in top['devices'].values() for value in interface_addresses(rec).values()]
    first=ipaddress.IPv4Network(preferred)
    if first.prefixlen!=prefix:raise ValueError('Несогласованная маска новой сети')
    if not any(first.overlaps(n) for n in used):return first
    for pool in ('10.240.0.0/16','172.31.0.0/16','192.168.0.0/16','198.18.0.0/15'):
        for candidate in ipaddress.IPv4Network(pool).subnets(new_prefix=prefix):
            if not any(candidate.overlaps(n) for n in used):return candidate
    raise ValueError('Не найдено свободное адресное пространство для новых связей')


def route_metric(data, device, target):
    address=ipaddress.IPv4Address(target)
    matches=[]
    for node in data.get('devices',[]):
        if node.get('device')==device:
            for entry in node.get('routes',[]):
                try:
                    prefix=ipaddress.IPv4Network(entry['prefix'])
                    if address in prefix: matches.append((prefix.prefixlen,entry))
                except (ValueError,KeyError): pass
    if not matches: return None
    return max(matches,key=lambda p:p[0])[1]


def changed_next_hops(before,after,routers,hosts):
    changed=[]
    for node in routers:
        for ip in hosts.values():
            old,new=route_metric(before,node,ip),route_metric(after,node,ip)
            if old and new and (old.get('interface'),old.get('next_hop'))!=(new.get('interface'),new.get('next_hop')):
                changed.append((node,ip,old,new))
    return changed


async def wait_changed_route(r,before,routers,hosts,rounds=12):
    # Knowing every prefix is not the same as completing a new SPF calculation.
    # Wait for the specific postcondition of this experiment instead of returning
    # as soon as the old, still complete routing table is observed.
    for _ in range(rounds):
        await r.settle(2)
        after=json_reply(await r.call('pt_read_routes',device=''))
        changed=changed_next_hops(before,after,routers,hosts)
        if changed:return after,changed
    return after,[]


async def read_routes(r,title):
    data=json_reply(await r.call('pt_read_routes',device=''))
    r.observe(title,data)
    return data


async def protocol(r,routers,kind):
    top=await r.snapshot()
    for node in routers:
        addresses=interface_addresses(top['devices'][node])
        commands=['no router rip','no router ospf 1']
        if kind=='rip':
            commands += ['router rip','version 2','no auto-summary']
            for address in addresses.values():
                commands.append('network '+str(ipaddress.IPv4Interface(address).network.network_address))
        else:
            commands += ['router ospf 1',f'router-id 1.1.{routers.index(node)//254}.{routers.index(node)%254+1}']
            for address in addresses.values():
                network=ipaddress.IPv4Interface(address).network
                commands.append(f'network {network.network_address} {network.hostmask} area 0')
        commands += ['exit']
        await r.config(node,commands)
        await r.cli(node,['write memory'])


async def clear_static(r,routers):
    for node in routers:
        config=await r.cli(node,['enable','show running-config'])
        static=[line.strip() for line in config.splitlines() if line.strip().startswith('ip route ')]
        if static: await r.config(node,['no '+line for line in static])
        reread=await r.cli(node,['show running-config'])
        if any(line.strip().startswith('ip route ') for line in reread.splitlines()):
            raise BridgeError('Статические маршруты остались на '+node)


async def serial_ports(r,node,need=1):
    top=await r.snapshot();rec=top['devices'][node]
    used={ep for link in top['links'] for ep in (link['a'],link['b'])}
    ports=re.findall(r'Serial\d+(?:/\d+)+',rec['ports'])
    available=[p for p in ports if (node,p) not in used]
    if len(available)>=need:return available[:need]
    model=rec['model']
    await r.call('pt_list_modules',router_model=model)
    if model in ('1941','2901','2911'):
        slots=['0/0','0/1'];module='HWIC-2T'
    elif model in ('ISR4321','ISR4331'):
        slots=['0/1','0/2'];module='NIM-2T'
    else:
        raise ValueError(f'Для serial нужен адаптер модели {model}; автоматическая установка проверяется отдельно')
    modules=[{'device':node,'slot':slot,'module':module} for slot in slots
             if not any(p.startswith('Serial'+slot+'/') for p in ports)]
    if not modules:raise ValueError('Недостаточно свободных serial-портов: '+node)
    await r.call('pt_install_modules_batch',modules=modules)
    await r.settle(2)
    top=await r.snapshot()
    ports=re.findall(r'Serial\d+(?:/\d+)+',top['devices'][node]['ports'])
    available=[p for p in ports if (node,p) not in used]
    if len(available)<need:raise BridgeError('Serial-порты после установки не подтверждены')
    return available[:need]


async def new_router_link(r,a,b,index,serial=False,addresses=None):
    top=await r.snapshot()
    if serial:
        pa=(await serial_ports(r,a))[0];pb=(await serial_ports(r,b))[0]
    else:
        pa=free_ports(top,a)[0];pb=free_ports(top,b)[0]
    await r.call('pt_add_link',device1=a,port1=pa,device2=b,port2=pb,cable_type='serial' if serial else 'cross')
    if not addresses:
        prefix=30 if serial else 24
        network=unused_network(top,prefix,f'172.30.{index}.0/{prefix}')
        addresses=(f'{network.network_address+1}/{prefix}',f'{network.network_address+2}/{prefix}')
    for node,port,address in ((a,pa,addresses[0]),(b,pb,addresses[1])):
        ip=ipaddress.IPv4Interface(address)
        lines=[f'interface {port}',f'ip address {ip.ip} {ip.netmask}']
        if serial and node==a:lines += ['clock rate 64000']
        if serial:lines += ['bandwidth 64']
        await r.config(node,lines+['no shutdown','exit'])
    return {'a':(a,pa),'b':(b,pb),'cable':'serial' if serial else 'cross'}


async def all_ping(r,hosts):
    outcomes=[]
    for a,b in itertools.combinations(hosts,2):
        ok,evidence=await r.ping(a,hosts[b]);outcomes.append(ok)
        r.observe(f'Проверка связности {a} → {b}',evidence)
    return bool(outcomes) and all(outcomes)


async def traceroutes(r,hosts,title):
    names=list(hosts)
    if len(names)<3:raise ValueError('Нужно минимум три ПК в разных сетях для трёх трассировок')
    pairs=[(names[0],names[1]),(names[1],names[2]),(names[2],names[0])]
    results=[]
    for a,b in pairs:
        text=await r.host(a,'tracert '+hosts[b])
        r.observe(title+f' {a} → {b}',text)
        ips=re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b',text)
        results.append(hosts[b] in ips and 'Trace complete' in text)
    return all(results)


async def wait_convergence(r,routers,protocol_letter,rounds=12):
    last={}
    for _ in range(rounds):
        await r.settle(2)
        last=json_reply(await r.call('pt_read_routes',device=''))
        records={d['device']:d for d in last.get('devices',[])}
        if all(any(x.get('protocol')==protocol_letter for x in records.get(n,{}).get('routes',[])) for n in routers) and not last.get('convergence_gaps'):
            return last
    return last


async def routing(r,request):
    top=await r.snapshot();routers=router_nodes(top);hosts=host_addresses(top)
    if len(routers)<3 or len(hosts)<3:
        raise ValueError('Нужен исходный .pkt лабораторной №5: минимум три роутера с ПК и адресами. Произвольная схема не подставляется.')
    await clear_static(r,routers)
    if request.lab==6:
        await rip(r,request,top,routers,hosts)
    else:
        await ospf(r,request,top,routers,hosts)


async def rip(r,request,top,routers,hosts):
    # RIP exercise recommends /24; renumber only point-to-point router links whose
    # original masks differ, so multiple /30 links do not become overlapping /24s.
    wan=[l for l in top['links'] if l['a'][0] in routers and l['b'][0] in routers]
    for i,link in enumerate(wan,1):
        old=[interface_addresses(top['devices'][n]).get(p) for n,p in (link['a'],link['b'])]
        if not all(x and ipaddress.IPv4Interface(x).network.prefixlen==24 for x in old):
            network=unused_network(await r.snapshot(),24,f'172.29.{i}.0/24')
            for j,(node,port) in enumerate((link['a'],link['b']),1):
                await r.config(node,[f'interface {port}',f'ip address {network.network_address+j} 255.255.255.0','no shutdown','exit'])
    r.requirement(0,True,'Продолжена исходная топология, статические маршруты удалены с read-back; WAN при необходимости переведены в уникальные /24')
    await protocol(r,routers,'rip')
    tables=await wait_convergence(r,routers,'R')
    configured=True
    for node in routers:
        text=await r.cli(node,['show ip protocols'])
        r.observe('RIP '+node,text);configured &= 'rip' in text.lower()
    r.requirement(1,configured,'RIPv2, network для всех адресованных интерфейсов, no auto-summary; show ip protocols сохранён')
    connected=await all_ping(r,hosts)
    r.observe('Таблицы RIP до добавления цикла',tables)
    r.requirement(2,connected and all(any(x.get('protocol')=='R' for x in d.get('routes',[])) for d in tables.get('devices',[]) if d.get('device') in routers),str(tables))
    r.requirement(3,await traceroutes(r,hosts,'До изменения топологии'),'Сохранены три реальные трассировки')
    dist=graph_distances(await r.snapshot(),routers)
    _,a,b=max((length,a,b) for a,ds in dist.items() for b,length in ds.items() if a<b)
    before=await read_routes(r,'Маршруты RIP до нового соединения')
    await new_router_link(r,a,b,250,serial=False)
    await protocol(r,routers,'rip')
    after=await wait_convergence(r,routers,'R')
    r.requirement(4,True,f'Добавлена связь {a} ↔ {b}, объявлена в RIP')
    reductions=[]
    for target in hosts.values():
        old,new=route_metric(before,a,target),route_metric(after,a,target)
        if old and new and isinstance(old.get('metric'),int) and isinstance(new.get('metric'),int) and new['metric']<old['metric']:
            reductions.append((target,old['metric'],new['metric']))
    await traceroutes(r,hosts,'После добавления цикла')
    r.requirement(5,bool(reductions) and await all_ping(r,hosts),str(reductions))
    outage=next(n for n in routers if n not in (a,b))
    r.restorations.append(('pt_device_power',{'device':outage,'on':True}))
    await r.call('pt_device_power',device=outage,on=False)
    await r.settle(8)
    failed_tables=await read_routes(r,'Таблицы RIP при выключенном '+outage)
    # Route-table evidence is accompanied by observed probes, never an assumption of resilience.
    probes=[]
    for x,y in list(itertools.combinations(hosts,2))[:3]:
        result=await r.call('pt_verify_connectivity',from_device=x,to_ip=hosts[y]);r.observe(f'Отказ {outage}: {x} → {y}',result);probes.append(result)
    r.requirement(6,bool(failed_tables.get('devices')) and len(probes)==3,'Питание выключено с read-back; записаны таблицы и измеренные результаты трёх ping')
    await r.call('pt_device_power',device=outage,on=True)
    await wait_convergence(r,routers,'R')
    r.requirement(7,await all_ping(r,hosts),'После включения роутера повторно проверена связность')
    await r.capture('RIP после восстановления')


async def ospf(r,request,top,routers,hosts):
    links=[l for l in top['links'] if l['a'][0] in routers and l['b'][0] in routers]
    if len(links)!=len(routers)-1:raise ValueError('Исходная сеть №5 должна иметь древовидную топологию роутеров')
    for i,link in enumerate(links,1):
        old=[interface_addresses(top['devices'][n]).get(p) for n,p in (link['a'],link['b'])]
        await r.call('pt_delete_link',device_name=link['a'][0],interface_name=link['a'][1])
        for node,port in (link['a'],link['b']):await r.config(node,[f'interface {port}','no ip address','shutdown','exit'])
        await new_router_link(r,link['a'][0],link['b'][0],i,serial=True,addresses=old if all(old) else None)
    r.requirement(0,all(l['cable']=='serial' for l in (await r.snapshot())['links'] if l['a'][0] in routers and l['b'][0] in routers), 'Межроутерные связи заменены на serial; clock rate 64000 на DCE')
    # Extend a leaf by three routers and three hosts, keeping a tree.
    degrees={n:sum(n in (l['a'][0],l['b'][0]) for l in links) for n in routers}
    parent=min(routers,key=lambda n:degrees[n])
    for i in range(1,4):
        node=f'OSPFExtra{i}';pc=f'OSPFHost{i}'
        if node in top['devices'] or pc in top['devices']:raise ValueError('Имена новых устройств уже заняты')
        await r.call('pt_add_device',name=node,model='2911',x=250+i*180,y=400)
        await r.call('pt_add_device',name=pc,model='PC-PT',x=250+i*180,y=600)
        await new_router_link(r,parent,node,200+i,serial=True)
        lan=unused_network(await r.snapshot(),24,f'192.168.{200+i}.0/24')
        await r.call('pt_add_link',device1=node,port1='GigabitEthernet0/0',device2=pc,port2='FastEthernet0',cable_type='cross')
        await r.config(node,['interface GigabitEthernet0/0',f'ip address {lan.network_address+1} 255.255.255.0','no shutdown','exit'])
        await r.call('pt_host_ip_config',device=pc,interface='FastEthernet0',mode='static',ip=str(lan.network_address+10),mask='255.255.255.0',gateway=str(lan.network_address+1),dns='')
        routers.append(node);hosts[pc]=str(lan.network_address+10);parent=node
    r.requirement(1,True,'Добавлены ровно три роутера 2911 с ПК, связи образуют продолжение дерева')
    await protocol(r,routers,'ospf')
    tables=await wait_convergence(r,routers,'O')
    r.requirement(2,await all_ping(r,hosts),'Настроены адреса и OSPF area 0; проверены все пары ПК')
    neighbors=json_reply(await r.call('pt_read_ospf',device=''))
    r.observe('Соседи OSPF каждого роутера',neighbors);r.observe('Таблицы OSPF каждого роутера',tables)
    r.requirement(3,bool(neighbors.get('devices')) and len(tables.get('devices',[]))>=len(routers),str(neighbors))
    r.requirement(4,await traceroutes(r,hosts,'OSPF исходное дерево'),'Ping выполнены, три трассировки записаны')
    top=await r.snapshot()
    wan=[l for l in top['links'] if l['a'][0] in routers and l['b'][0] in routers]
    cut=None;components=None
    for link in wan:
        trial={**top,'links':[l for l in top['links'] if l!=link]}
        ds=graph_distances(trial,routers)
        left=set(ds[routers[0]]);right=set(routers)-left
        if len(left)>=2 and len(right)>=2:
            cut=link;components=(left,right);break
    if not cut:raise ValueError('Нет линии, разделяющей дерево на компоненты минимум по два роутера')
    node,port=cut['a']
    r.restorations.append(('pt_cli',{'device':node,'commands':['enable','configure terminal',f'interface {port}','no shutdown','end']}))
    await r.config(node,[f'interface {port}','shutdown','exit']);await r.settle(5)
    r.requirement(5,True,str(components))
    # Learn attachment router by graph distance from each host through switches.
    def host_component(host):
        ds=graph_distances(top,list(top['devices']))[host]
        nearest=min(routers,key=lambda n:ds.get(n,100000))
        return 0 if nearest in components[0] else 1
    checks=[]
    for a,b in itertools.combinations(hosts,2):
        expected=host_component(a)==host_component(b)
        ok,evidence=await r.ping(a,hosts[b],expected=expected)
        checks.append(ok);r.observe(f'Разделённая сеть {a} → {b}',evidence)
    r.requirement(6,all(checks),'Измерена связь внутри компонентов и её отсутствие между компонентами')
    await r.config(node,[f'interface {port}','no shutdown','exit']);await r.settle(4)
    a=next(n for n in components[0] if n!=node);b=next(n for n in components[1] if n!=cut['b'][0])
    redundant=await new_router_link(r,a,b,249,serial=True)
    await protocol(r,routers,'ospf');await wait_convergence(r,routers,'O')
    r.requirement(7,await all_ping(r,hosts),f'Добавлена резервная связь {redundant}')
    before=await read_routes(r,'OSPF до изменения bandwidth')
    await r.config(node,[f'interface {port}','bandwidth 8','exit'])
    for n,p in (redundant['a'],redundant['b']):await r.config(n,[f'interface {p}','bandwidth 10000','exit'])
    after,changed=await wait_changed_route(r,before,routers,hosts)
    r.observe('OSPF после пересчёта SPF по bandwidth',after)
    r.observe('Изменение выбранных маршрутов после bandwidth',changed)
    r.requirement(8,bool(changed),'Изменение next-hop/интерфейса в реальных таблицах: '+str(changed))
    await r.config(node,[f'interface {port}','shutdown','exit']);await wait_convergence(r,routers,'O')
    r.requirement(9,await all_ping(r,hosts),'Повторён тот же отказ после добавления резерва, проверены все пары ПК')
    await r.config(node,[f'interface {port}','no shutdown','exit']);await r.settle(4)
    await r.capture('OSPF с резервным serial-соединением')

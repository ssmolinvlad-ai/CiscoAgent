from __future__ import annotations

import itertools
import json
import re
import secrets

from .bridge import BridgeError, json_reply
from .coursework import VARIANTS
from .executor import Executor
from .models import Check, Device, Endpoint, Interface, LabPlan, Link


SCHEMES = {
    1: [('SW1','MS'), ('SW2','MS'), ('SW3','MS')],
    2: [('SW2','SW1'), ('SW1','MS'), ('MS','SW3')],
    3: [('SW1','SW2'), ('SW1','MS'), ('SW1','SW3')],
    4: [('SW2','SW1'), ('SW1','MS'), ('MS','SW3')],
}


def switching_plan(variant):
    scheme, *values = VARIANTS[variant]
    counts, routed = values[:3], set(values[3])
    switch_models = {'SW1':'2960-24TT', 'SW2':'3560-24PS' if scheme == 4 else '2960-24TT',
                     'SW3':'2960-24TT', 'MS':'3560-24PS'}
    positions={'SW1':(140,220),'SW2':(480,220),'SW3':(820,220),'MS':(480,60)}
    devices = {n: Device(name=n, model=m, kind='switch', x=positions[n][0], y=positions[n][1],
                         vlans={10:'GROUP1',20:'GROUP2',30:'GROUP3'}) for i,(n,m) in enumerate(switch_models.items())}
    links, next_port = [], dict.fromkeys(devices, 1)
    for a,b in SCHEMES[scheme]:
        pa,pb=f'FastEthernet0/{next_port[a]}',f'FastEthernet0/{next_port[b]}'
        next_port[a]+=1; next_port[b]+=1
        links.append(Link(a=Endpoint(device=a,port=pa),b=Endpoint(device=b,port=pb),cable='cross'))
        for node,port in ((a,pa),(b,pb)):
            if switch_models[node]=='3560-24PS':
                devices[node].ios += [f'interface {port}','switchport trunk encapsulation dot1q','exit']
            devices[node].ios += [f'interface {port}','switchport mode trunk','switchport trunk allowed vlan 10,20,30','no shutdown','exit']
    hosts_by_group = {1:[],2:[],3:[]}
    host_slots={1:0,2:0,3:0}
    for group, per_switch in enumerate(counts,1):
        octet = int(('' if group==1 else str(group-1))+str(variant))
        host_index=10
        for sw_index,count in enumerate(per_switch,1):
            sw=f'SW{sw_index}'
            for _ in range(count):
                name=f'V{group}PC{host_index}'
                address=f'192.168.{octet}.{host_index}/24'
                host_index+=1
                port=f'FastEthernet0/{next_port[sw]}'; next_port[sw]+=1
                devices[sw].interfaces.append(Interface(name=port,vlan=group*10))
                devices[sw].ios += [f'interface {port}','spanning-tree portfast','exit']
                slot=host_slots[sw_index];host_slots[sw_index]+=1
                devices[name]=Device(name=name,model='PC-PT',kind='pc',x=80+(sw_index-1)*340+(slot%2)*120,y=350+(slot//2)*130,
                    interfaces=[Interface(name='FastEthernet0',address=address)],
                    gateway=f'192.168.{octet}.254' if group in routed else None)
                links.append(Link(a=Endpoint(device=name,port='FastEthernet0'),b=Endpoint(device=sw,port=port),cable='straight'))
                hosts_by_group[group].append(name)
        if group in routed:
            devices['MS'].interfaces.append(Interface(name=f'Vlan{group*10}',address=f'192.168.{octet}.254/24'))
    devices['MS'].ios += ['ip routing']
    first=next(names for names in hosts_by_group.values() if len(names)>=2)
    target=devices[first[1]].interfaces[0].address.split('/')[0]
    plan=LabPlan(title=f'Лабораторная 3 VLAN вариант {variant}',filename='lab-3-vlans.pkt',devices=list(devices.values()),links=links,
        checks=[Check(id='baseline_vlan',description='Базовая связь внутри VLAN',kind='ping',device=first[0],target=target)],
        assumptions=['Номера трёх рабочих VLAN: 10, 20, 30. Обозначения VLAN 1/2/3 в таблице означают группы; рабочий VLAN1 не используется.'])
    return plan, hosts_by_group, routed


async def fresh(runner):
    await runner.call('pt_send_raw',js_code="try { reportResult(JSON.stringify({ok:ipc.appWindow().fileNew(false)===true})); } catch(e) { reportResult('ERROR:'+e); }",wait_result=True)
    state=await runner.snapshot()
    if state['devices'] or state['links']:
        raise BridgeError('Новая рабочая область не подтверждена')


async def switching(r, request):
    await fresh(r)
    await r.call('pt_add_device',name='ConsolePC',model='PC-PT',x=120,y=250)
    await r.call('pt_add_device',name='ConsoleSW',model='2960-24TT',x=380,y=150)
    await r.call('pt_add_link',device1='ConsolePC',port1='RS 232',device2='ConsoleSW',port2='Console',cable_type='console')
    console=request.console_password or secrets.token_urlsafe(10).replace('-','a')
    enable=request.enable_password or secrets.token_urlsafe(10).replace('-','b')
    r.secrets += [console,enable]
    (r.folder/'credentials.local.json').write_text(json.dumps({'console_vty':console,'enable':enable,'hostname':request.hostname}),encoding='utf-8')
    commands=['enable','configure terminal',f'hostname {request.hostname}',f'interface Vlan1',
              f'ip address 192.168.{request.variant}.254 255.255.255.0','no shutdown','exit',
              'banner motd #Educational lab access only#',f'enable secret {enable}',
              'line console 0',f'password {console}','login','exit',
              'line vty 0 4',f'password {console}','login','transport input telnet','exit','end','write memory']
    terminal=await r.call('pt_terminal',device='ConsolePC',commands=commands,auto_confirm=True)
    r.observe('Настройка через Console и Terminal',terminal)
    terminal_ok='[ok]' in terminal and not re.search(r'\[(ERROR|UNKNOWN COMMAND|TIMEOUT|WAITING FOR ANSWER)\]',terminal)
    r.requirement(0,terminal_ok,'Настройка передана через Terminal ПК и настоящий Console-кабель')
    running=await r.cli('ConsoleSW',['show running-config'])
    needed=[f'hostname {request.hostname}',f'192.168.{request.variant}.254','line con 0','line vty 0 4','login','banner motd','enable secret']
    r.requirement(1,all(s in running for s in needed),r.clean(running))
    await r.call('pt_delete_link',device_name='ConsolePC',interface_name='RS 232')
    await r.call('pt_add_link',device1='ConsolePC',port1='FastEthernet0',device2='ConsoleSW',port2='FastEthernet0/1',cable_type='straight')
    await r.call('pt_host_ip_config',device='ConsolePC',interface='FastEthernet0',mode='static',ip=f'192.168.{request.variant}.10',mask='255.255.255.0',gateway='',dns='')
    await r.settle()
    top=await r.snapshot()
    r.requirement(2,any(l['cable']=='straight' for l in top['links']) and all(l['cable']!='console' for l in top['links']),str(top['links']))
    telnet=await r.host('ConsolePC',f'telnet 192.168.{request.variant}.254',inputs=[console,'enable',enable,'show privilege','exit','exit'])
    r.observe('Проверка TELNET и enable',telnet)
    r.requirement(3,bool(re.search(r'privilege level is 15',telnet,re.I)),telnet)
    await r.save_native('lab-3-console.pkt')
    await r.capture('Console и TELNET после настройки')
    plan,groups,routed=switching_plan(request.variant)
    r.observe('План VLAN',plan.model_dump_json(indent=2))
    base_dir=r.folder/'vlan-build'
    baseline=await Executor(r.bridge,base_dir,r.control,r.on_stage,r.on_log).run(plan,{})
    if not baseline.project:
        raise BridgeError('Не удалось построить VLAN: '+'; '.join(baseline.errors))
    await r.settle(4)
    top=await r.snapshot()
    correct=len(top['devices'])==len(plan.devices) and len(top['links'])==len(plan.links)
    r.requirement(4,correct,f'Вариант {request.variant}; схема {VARIANTS[request.variant][0]}; устройств {len(top["devices"])}; связей {len(top["links"])}')
    # show vlan is explicitly required by this assignment.
    vlan_evidence={}
    vlan_ok=True
    for dev in plan.devices:
        if dev.kind=='switch':
            actual=await r.cli(dev.name,['show vlan brief'])
            r.observe('show vlan '+dev.name,actual)
            vlan_evidence[dev.name]=actual
            vlan_ok &= verify_access_vlans(actual,dev.interfaces)
    r.requirement(5,vlan_ok,json.dumps(vlan_evidence,ensure_ascii=False))
    lookup={d.name:d for d in plan.devices}
    within=True
    for names in groups.values():
        for a,b in itertools.combinations(names,2):
            ok,evidence=await r.ping(a,lookup[b].interfaces[0].address.split('/')[0])
            r.observe(f'Внутри VLAN {a} → {b}',evidence); within &= ok
    r.requirement(6,within,'Проверены все пары ПК внутри каждого VLAN')
    allowed,denied=True,True
    for ga,gb in itertools.permutations(groups,2):
        a,b=groups[ga][0],groups[gb][0]
        expected=ga in routed and gb in routed
        ok,evidence=await r.ping(a,lookup[b].interfaces[0].address.split('/')[0],expected=expected)
        r.observe(f'Между группами VLAN {ga} → {gb}',evidence)
        if expected: allowed &= ok
        else: denied &= ok
    r.requirement(7,allowed,f'Маршрутизация разрешена между группами {sorted(routed)}')
    r.requirement(8,denied,'Для остальных направлений измерено отсутствие ответов; тайм-аут инструмента не считается доказательством')
    await r.capture('Итоговая топология VLAN')


def verify_access_vlans(output,interfaces):
    """Compare observed IOS membership, including wrapped rows, with each access port."""
    memberships={}
    current=None
    for line in output.splitlines():
        row=re.match(r'^\s*(\d+)\s+\S+\s+(?:active|suspended)\s*(.*)$',line)
        if row:
            current=int(row[1]); memberships[current]=set()
            line=row[2]
        if current is not None:
            memberships[current].update(re.findall(r'\b(?:Fa|Gig|Gi|Te)\d+(?:/\d+)+\b',line))
    for interface in interfaces:
        if interface.vlan is None:
            continue
        short=interface.name.replace('FastEthernet','Fa').replace('GigabitEthernet','Gig')
        if interface.vlan not in (10,20,30) or short not in memberships.get(interface.vlan,set()):
            return False
        if short in memberships.get(1,set()):
            return False
    return all(vlan in memberships for vlan in (10,20,30))


def switch_nodes(top):
    return [n for n,d in top['devices'].items() if d['model'].startswith(('2960','3560','2950','3650'))]


def free_ports(top,node,count=1):
    used={ep for link in top['links'] for ep in (link['a'],link['b'])}
    ports=re.findall(r'(?:FastEthernet|GigabitEthernet)\d+(?:/\d+)+',top['devices'][node]['ports'])
    available=[p for p in ports if (node,p) not in used]
    if len(available)<count:
        raise ValueError(f'У {node} нет {count} свободных Ethernet-портов')
    return available[:count]


async def add_trunk(r,a,pa,b,pb):
    await r.call('pt_add_link',device1=a,port1=pa,device2=b,port2=pb,cable_type='cross')
    top=await r.snapshot()
    for node,port in ((a,pa),(b,pb)):
        lines=[f'interface {port}']
        if top['devices'][node]['model'].startswith('3560'):
            lines += ['switchport trunk encapsulation dot1q']
        lines += ['switchport mode trunk','no shutdown','exit']
        await r.config(node,lines)


async def resilience(r,request):
    from .course_runner import graph_distances, host_addresses, echo_received
    top=await r.snapshot(); switches=switch_nodes(top); hosts=host_addresses(top)
    if len(switches)<3 or len(hosts)<3:
        raise ValueError('Для №4 нужна готовая сеть №3 с минимум тремя коммутаторами и тремя ПК')
    distances=graph_distances(top,switches)
    pairs=[(distance,a,b) for a,dist in distances.items() for b,distance in dist.items() if a<b]
    distance,a,b=max(pairs)
    pa,pb=free_ports(top,a)[0],free_ports(top,b)[0]
    await r.call('pt_simulation_mode',on=True)
    await r.call('pt_simulation_step',action='reset',times=1)
    await add_trunk(r,a,pa,b,pb)
    r.requirement(0,distance>=2,f'Добавлена линия {a}:{pa} ↔ {b}:{pb}, расстояние до добавления {distance}')
    await r.call('pt_simulation_step',action='forward',times=50)
    stp_trace=json_reply(await r.call('pt_read_packet_trace',limit=200,include_decisions=True))
    r.observe('События STP при добавлении резерва',stp_trace)
    await r.settle(4)
    stp=json_reply(await r.call('pt_read_stp',switch=''))
    r.observe('Корни STP до изменения',stp)
    r.requirement(1,any(f.get('traffic_type')=='STP' for f in stp_trace.get('trace',[])) and
                  bool(stp.get('devices')) and not stp.get('root_disagreements') and not stp.get('multiple_roots'),str(stp))
    # Choose a same-subnet remote pair whose first-hop switches differ.
    attached={}
    for link in top['links']:
        for x,y in ((link['a'][0],link['b'][0]),(link['b'][0],link['a'][0])):
            if x in hosts and y in switches: attached[x]=y
    candidates=[(x,y) for x,y in itertools.combinations(hosts,2) if hosts[x].rsplit('.',1)[0]==hosts[y].rsplit('.',1)[0]
                and attached.get(x)!=attached.get(y)]
    if not candidates: raise ValueError('Нужны ПК одного VLAN на разных коммутаторах')
    source,destination=candidates[0]
    baseline=await r.trace(source,destination)
    await r.realtime()
    forwarding=[f for f in baseline if f.get('device') in switches and f.get('out_port') and f.get('traffic_type')=='ICMP']
    current_links=(await r.snapshot())['links']
    cut=next((f for f in forwarding if any({l['a'][0],l['b'][0]}<=set(switches) and
             (f['device'],f['out_port']) in (l['a'],l['b']) for l in current_links)),None)
    if not cut: raise BridgeError('Фактический путь ICMP не содержит отключаемого межкоммутаторного порта')
    node,port=cut['device'],cut['out_port']
    restore={'device':node,'commands':['enable','configure terminal',f'interface {port}','no shutdown','end']}
    r.restorations.append(('pt_cli',restore))
    # Persistent ping is kept alive while the shutdown is performed, then Ctrl+C is sent.
    from .course_traffic import continuous_ping
    async with continuous_ping(r,source,hosts[destination]) as monitor:
        await r.config(node,[f'interface {port}','shutdown','exit'])
        await r.settle(4)
    r.observe('Непрерывный ping во время отказа порта',monitor['output'])
    r.requirement(2,monitor['started'] and monitor['stopped'] and 'Reply from' in monitor['output'],monitor['output'])
    alternative=await r.trace(source,destination)
    avoided=not any(f.get('device')==node and f.get('out_port')==port and f.get('traffic_type')=='ICMP' for f in alternative)
    r.requirement(3,echo_received(alternative,source) and avoided,f'Выключен {node}:{port}; новый путь обходит его: {avoided}')
    await r.realtime(); await r.config(node,[f'interface {port}','no shutdown','exit'])
    roots=[d['device'] for d in stp['devices'] if any(i.get('is_root') for i in d.get('instances',[]))]
    chosen=next(n for n in switches if n not in roots)
    vlans=sorted({i['vlan'] for d in stp['devices'] for i in d.get('instances',[])})
    await r.config(chosen,[f'spanning-tree vlan {",".join(map(str,vlans))} priority 0'])
    await r.settle(4)
    moved=json_reply(await r.call('pt_read_stp',switch=''))
    r.requirement(4,any(d['device']==chosen and any(i.get('is_root') for i in d.get('instances',[])) for d in moved.get('devices',[])),str(moved))
    await etherchannel(r,await r.snapshot(),hosts,attached)
    await r.capture('Итоговая отказоустойчивая сеть')


async def etherchannel(r,top,hosts,attached):
    from .course_runner import echo_received
    grouped={}
    for host,sw in attached.items():
        grouped.setdefault((sw,hosts[host].rsplit('.',1)[0]),[]).append(host)
    group=next(((sw,network,names) for (sw,network),names in grouped.items() if len(names)>=2 and
                any(attached.get(h)!=sw and ip.rsplit('.',1)[0]==network for h,ip in hosts.items())),None)
    if not group: raise ValueError('Для EtherChannel нет двух локальных ПК и удалённого ПК одного VLAN')
    sw,network,sources=group
    remote=next(h for h,ip in hosts.items() if attached.get(h)!=sw and ip.rsplit('.',1)[0]==network)
    neighbor=next((l['b'][0] if l['a'][0]==sw else l['a'][0] for l in top['links'] if sw in (l['a'][0],l['b'][0]) and
                   l['a'][0] in switch_nodes(top) and l['b'][0] in switch_nodes(top)),None)
    if not neighbor: raise ValueError('Не найден соседний коммутатор')
    left,right=free_ports(top,sw,2),free_ports(top,neighbor,2)
    for pa,pb in zip(left,right): await add_trunk(r,sw,pa,neighbor,pb)
    # Prefer the aggregate to other paths; verify the actual selected member in traces.
    for node,ports in ((sw,left),(neighbor,right)):
        await r.config(node,[f'interface range {ports[0]} - {ports[1].split("/")[-1]}','channel-group 1 mode active','exit',
                             'interface Port-channel1','switchport mode trunk','spanning-tree cost 1','exit',
                             'port-channel load-balance src-mac'])
    await r.settle(5)
    summaries=[]
    for node in (sw,neighbor): summaries.append(await r.cli(node,['show etherchannel summary','show etherchannel load-balance']))
    r.observe('Состояние EtherChannel', '\n'.join(summaries))
    r.requirement(5,all('Po1(SU)' in s and s.count('(P)')>=2 for s in summaries),'\n'.join(summaries))
    results={}
    # Two factory MACs can hash to the same member even with src-mac. The task
    # explicitly asks to achieve parallel forwarding: use two distinct local
    # unicast MACs and measure which physical member each flow actually takes.
    for index,source in enumerate(sources[:2],1):
        address=f'0200.0000.000{index}'
        await r.call('pt_set_port',device=source,interface='FastEthernet0',mac_address=address)
        r.observe('MAC для опыта EtherChannel '+source,await r.call('pt_read_device_panel',device=source))
    for host in hosts:
        await r.host(host,'arp -d')
    for algorithm in ('src-mac','dst-mac'):
        await r.realtime()
        for node in (sw,neighbor): await r.config(node,[f'port-channel load-balance {algorithm}'])
        await r.settle(2)
        for source in sources[:2]: await r.ping(source,hosts[remote])
        # A new PDU scenario isolates this measurement from baseline/failover
        # PDUs which Reset Simulation would otherwise replay in the background.
        await r.call('pt_send_raw',js_code="(function(){try{ipc.appWindow().getUserCreatedPDU().newScenarioBtn_clicked();reportResult(JSON.stringify({created:true}));}catch(e){reportResult('ERROR:'+e);}})();",wait_result=True)
        await r.call('pt_simulation_mode',on=True)
        await r.call('pt_simulation_step',action='reset',times=1)
        # One JS call gives both PDUs the same simulated start instant.
        cfg=json.dumps({'sources':sources[:2],'destination':remote})
        script=f"(function(){{try{{var c={cfg};var u=ipc.appWindow().getUserCreatedPDU();var t=ipc.simulation().getCurrentSimTime();var codes=[];for(var i=0;i<c.sources.length;i++)codes.push(u.addSimplePdu(c.sources[i],c.destination));reportResult(JSON.stringify({{codes:codes,time:t}}));}}catch(e){{reportResult('ERROR:'+e);}}}})();"
        await r.call('pt_send_raw',js_code=script,wait_result=True)
        await r.call('pt_simulation_step',action='forward',times=45)
        data=json_reply(await r.call('pt_read_packet_trace',limit=200,include_decisions=True))
        r.observe('Одновременные потоки '+algorithm,data)
        frames=data.get('trace',[])
        members={source:{f.get('out_port') for f in frames if f.get('device')==sw and f.get('source')==source
                       and f.get('traffic_type')=='ICMP' and f.get('out_port') in left} for source in sources[:2]}
        reached=all(echo_received(frames,s) for s in sources[:2])
        times={source:min((f.get('sim_time') for f in frames if f.get('device')==sw and f.get('source')==source
               and f.get('traffic_type')=='ICMP' and f.get('out_port') in left),default=None) for source in sources[:2]}
        results[algorithm]=(members,reached,times)
    src,ok1,times1=results['src-mac']; dst,ok2,times2=results['dst-mac']
    first=[next(iter(src[s])) if len(src[s])==1 else None for s in sources[:2]]
    second=[next(iter(dst[s])) if len(dst[s])==1 else None for s in sources[:2]]
    simultaneous=all(t is not None for t in times1.values()) and len(set(times1.values()))==1
    sequential=all(t is not None for t in times2.values()) and len(set(times2.values()))==2
    r.requirement(6,ok1 and None not in first and first[0]!=first[1] and simultaneous,str({'members':src,'send_times':times1}))
    r.requirement(7,ok2 and None not in second and second[0]==second[1] and sequential,str({'members':dst,'send_times':times2}))
    await r.realtime()

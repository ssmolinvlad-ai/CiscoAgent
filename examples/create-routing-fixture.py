"""Create a disposable test fixture, not a replacement for a student's lab 5."""
import asyncio
from pathlib import Path

from ptagent.bridge import connect_bridge
from ptagent.control import RunControl
from ptagent.executor import Executor
from ptagent.gui import require_open_cisco
from ptagent.models import LabPlan,Device,Interface,Link,Endpoint,Check


def fixture_plan():
    devices=[];links=[]
    for i in range(1,4):
        ios=[]
        if i==1:ios=['ip route 192.168.12.0 255.255.255.0 10.10.1.2','ip route 192.168.13.0 255.255.255.0 10.10.1.2']
        if i==2:ios=['ip route 192.168.11.0 255.255.255.0 10.10.1.1','ip route 192.168.13.0 255.255.255.0 10.10.2.2']
        if i==3:ios=['ip route 192.168.11.0 255.255.255.0 10.10.2.1','ip route 192.168.12.0 255.255.255.0 10.10.2.1']
        devices += [Device(name=f'R{i}',kind='router',model='2911',x=120+(i-1)*300,y=120,
                           interfaces=[Interface(name='GigabitEthernet0/0',address=f'192.168.{10+i}.1/24')],ios=ios),
                    Device(name=f'PC{i}',kind='pc',model='PC-PT',x=120+(i-1)*300,y=380,
                           interfaces=[Interface(name='FastEthernet0',address=f'192.168.{10+i}.10/24')],gateway=f'192.168.{10+i}.1')]
        links.append(Link(a=Endpoint(device=f'R{i}',port='GigabitEthernet0/0'),b=Endpoint(device=f'PC{i}',port='FastEthernet0'),cable='cross'))
    lookup={d.name:d for d in devices}
    for index,a,pa,b,pb in [(1,'R1','GigabitEthernet0/1','R2','GigabitEthernet0/1'),(2,'R2','GigabitEthernet0/2','R3','GigabitEthernet0/1')]:
        lookup[a].interfaces.append(Interface(name=pa,address=f'10.10.{index}.1/24'))
        lookup[b].interfaces.append(Interface(name=pb,address=f'10.10.{index}.2/24'))
        links.append(Link(a=Endpoint(device=a,port=pa),b=Endpoint(device=b,port=pb),cable='cross'))
    return LabPlan(title='Тестовый стенд маршрутизации — не лабораторная 5',filename='routing-fixture.pkt',devices=devices,links=links,
                   checks=[Check(id='connected',description='Связь PC1 → PC3',kind='ping',device='PC1',target='192.168.13.10')],
                   assumptions=['Искусственный стенд для проверки исполнителя. Не выдаётся за топологию из лабораторной 5.'])


async def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    folder=Path(args.output).resolve();folder.mkdir(parents=True,exist_ok=False)
    desktop=require_open_cisco()
    async with connect_bridge(folder,print) as bridge:
        report=await Executor(bridge,folder,RunControl(),lambda *x:None,print).run(fixture_plan(),{},desktop)
        print(report.status,report.project)
    if not report.project:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())

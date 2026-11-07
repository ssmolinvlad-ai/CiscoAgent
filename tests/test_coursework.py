import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ptagent.bridge import BridgeError, MCPBridge
from ptagent.bridge import ensure_reply
from ptagent.control import RunControl, Stopped
from ptagent.coursework import CourseRequest, recognize_coursework, VARIANTS
from ptagent.course_switching import switching_plan, verify_access_vlans
from ptagent.course_runner import CourseRunner, graph_distances, host_addresses, echo_received
from ptagent.course_routing import route_metric
from ptagent.course_routing import wait_changed_route
from ptagent.course_routing import unused_network
from ptagent.models import RunReport,CheckResult
from ptagent.course_report import write_course_report


def test_vlan_readback_rejects_wrong_live_membership():
    plan,_,_=switching_plan(4)
    switch=next(d for d in plan.devices if d.name=='SW1')
    observed='1 default active Fa0/6\n10 GROUP1 active Fa0/3\n20 GROUP2 active Fa0/4,\n    Fa0/5\n30 GROUP3 active\n'
    assert verify_access_vlans(observed,switch.interfaces)
    assert not verify_access_vlans(observed.replace('Fa0/4','Fa0/14'),switch.interfaces)
    assert not verify_access_vlans('Pass the switch name.',switch.interfaces)


@pytest.mark.parametrize('variant',range(1,21))
def test_variant_counts_ports_and_allowed_routing(variant):
    plan,groups,routed=switching_plan(variant)
    row=VARIANTS[variant]
    assert [len(groups[n]) for n in (1,2,3)]==[sum(x) for x in row[1:4]]
    assert routed==set(row[4])
    assert {int(i.name[4:])//10 for d in plan.devices if d.name=='MS' for i in d.interfaces}==routed
    assert all(i.vlan!=1 for d in plan.devices for i in d.interfaces)
    for group,counts in enumerate(row[1:4],1):
        for sw,count in enumerate(counts,1):
            actual=sum(link.a.device in groups[group] and link.b.device==f'SW{sw}' for link in plan.links)
            assert actual==count


def test_variant_four_uses_two_multilayer_switches_and_exact_networks():
    plan,groups,routed=switching_plan(4)
    assert sum(d.model=='3560-24PS' for d in plan.devices)==2
    assert len(plan.devices)==16 and len(plan.links)==15
    assert {i.address.split('.')[2] for d in plan.devices if d.kind=='pc' for i in d.interfaces}=={'4','14','24'}
    assert routed=={1,3}
    assert all(not d.gateway for d in plan.devices if d.name in groups[2])


def test_recognition_does_not_trust_filename_alone(tmp_path):
    path=tmp_path/'2. Изучение протокол.pdf'
    path.write_bytes(b'not the original document')
    assert recognize_coursework(path,'Unrelated assignment') is None
    assert recognize_coursework(path,'7. Протокол динамической маршрутизации OSPF')==7


def test_route_comparison_uses_longest_prefix_not_default():
    data={'devices':[{'device':'R1','routes':[
        {'prefix':'0.0.0.0/0','metric':1}, {'prefix':'192.168.4.0/24','metric':3}]}]}
    assert route_metric(data,'R1','192.168.4.10')['metric']==3
    assert route_metric(data,'R2','192.168.4.10') is None


def test_spf_wait_does_not_accept_old_complete_table():
    before={'devices':[{'device':'R1','routes':[{'prefix':'192.168.4.0/24','next_hop':'10.0.0.1','interface':'Serial0/0/0','metric':2}]}]}
    after={'devices':[{'device':'R1','routes':[{'prefix':'192.168.4.0/24','next_hop':'10.1.0.1','interface':'Serial0/0/1','metric':5}]}]}
    class Runner:
        def __init__(self): self.reads=0
        async def settle(self,*args):pass
        async def call(self,*args,**kwargs):
            self.reads+=1
            return json.dumps(before if self.reads<3 else after)
    runner=Runner()
    result,changed=asyncio.run(wait_changed_route(runner,before,['R1'],{'PC':'192.168.4.10'},rounds=4))
    assert runner.reads==3 and changed and result==after


def test_new_links_never_overlap_a_preexisting_supernet():
    top={'devices':{'R1':{'ports':'GigabitEthernet0/0=172.30.1.1/255.255.0.0,GigabitEthernet0/1=10.0.0.1/255.0.0.0'}}}
    result=unused_network(top,30,'172.30.250.0/30')
    import ipaddress
    assert not result.overlaps(ipaddress.IPv4Network('172.30.0.0/16'))
    assert not result.overlaps(ipaddress.IPv4Network('10.0.0.0/8'))


def test_trace_with_only_sent_packets_does_not_prove_delivery():
    assert not echo_received([{'device':'PC1','sent':True,'traffic_type':'ICMP'}],'PC1')
    assert not echo_received([{'device':'PC2','accepted':True,'decisions':[{'description':'received an Echo Reply'}]}],'PC1')


def test_packet_loss_is_observation_but_transport_error_still_raises():
    ensure_reply(json.dumps({'output':'Request timed out. Reply from 192.168.1.1'}))
    with pytest.raises(BridgeError):ensure_reply('No response: bridge timed out')
    with pytest.raises(BridgeError):ensure_reply(json.dumps({'ok':False,'error':'timeout'}))


def test_graph_components_and_farthest_endpoints():
    top={'links':[{'a':('A','p'),'b':('B','p')},{'a':('B','q'),'b':('C','p')}]}
    assert graph_distances(top,['A','B','C','D'])['A']=={'A':0,'B':1,'C':2}


def test_stop_restores_temporary_outage_and_keeps_requirements_unverified(tmp_path):
    class Bridge:
        def __init__(self): self.calls=[]
        async def call(self,name,**args):
            self.calls.append((name,args))
            if name=='pt_bridge_status': return 'CONNECTED over HTTP'
            if name=='pt_save_project':
                (Path(args['directory'])/args['filename']).write_bytes(b'native fixture'*20)
            return '{}'
    class Runner(CourseRunner):
        async def arp(self,request):
            self.restorations.append(('pt_device_power',{'device':'R2','on':True}))
            raise Stopped('Stopped during outage')
    bridge=Bridge()
    report=asyncio.run(Runner(bridge,tmp_path,RunControl()).run_course(CourseRequest(lab=2),{}))
    assert report.status=='stopped'
    assert ('pt_device_power',{'device':'R2','on':True}) in bridge.calls
    assert all(c.status=='unverified' for c in report.checks)
    assert (tmp_path/'report.docx').is_file()


def test_word_report_preserves_failures_and_full_evidence_reference(tmp_path):
    from docx import Document
    report=RunReport(title='ARP',started_at='now',status='failed',checks=[
        CheckResult(id='req',description='ARP Reply',status='unverified',evidence='Нет наблюдения')])
    write_course_report(tmp_path,report,[{'title':'Трасса','evidence':'x'*15000}])
    text='\n'.join(p.text for p in Document(tmp_path/'report.docx').paragraphs)
    assert 'Не подтверждено' in text and 'observations.json' in text


def test_credentials_cannot_inject_commands():
    from pydantic import ValidationError
    with pytest.raises(ValidationError): CourseRequest(lab=3,console_password='abc\nreload')


def test_interactive_prompts_require_answers_and_final_host_prompt(tmp_path):
    class Bridge:
        async def call(self,*args,**kwargs):
            return 'PC (Command Prompt) — 2 command(s):\n> telnet 1.1.1.1 [WAITING FOR ANSWER]\nPassword:\n> answer [ok]\nFinal prompt: Password: (mode: user)'
    with pytest.raises(BridgeError):
        asyncio.run(CourseRunner(Bridge(),tmp_path,RunControl()).host('PC','telnet 1.1.1.1',inputs=['answer']))

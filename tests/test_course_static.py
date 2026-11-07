import asyncio
import runpy
from pathlib import Path
import pytest

from ptagent.course_static import static_routes
from ptagent.coursework import CourseRequest,recognize_coursework
from ptagent.course_routing import traceroutes


def plan():
    fixture=runpy.run_path(str(Path(__file__).parents[1]/'examples/create-routing-fixture.py'))['fixture_plan']()
    for device in fixture.devices:device.ios=[]
    return fixture


def test_static_next_hops_include_return_paths_and_remote_wan():
    routes=static_routes(plan())
    assert 'ip route 192.168.13.0 255.255.255.0 10.10.1.2' in routes['R1']
    assert 'ip route 192.168.11.0 255.255.255.0 10.10.2.1' in routes['R3']
    assert 'ip route 10.10.2.0 255.255.255.0 10.10.1.2' in routes['R1']
    assert not any('192.168.12.0' in line for line in routes['R2'])


def test_static_rejects_disconnected_graph_and_wrong_gateway():
    candidate=plan();candidate.links=[l for l in candidate.links if {l.a.device,l.b.device}!={'R2','R3'}]
    with pytest.raises(ValueError):static_routes(candidate)
    candidate=plan();next(d for d in candidate.devices if d.name=='PC1').gateway='192.168.11.254'
    with pytest.raises(ValueError,match='Шлюз'):static_routes(candidate)


def test_static_rejects_preinstalled_routes_and_wrong_masks():
    candidate=plan();candidate.devices[0].ios=['ip route 0.0.0.0 0.0.0.0 10.10.1.2']
    with pytest.raises(ValueError,match='без IOS'):static_routes(candidate)
    candidate=plan();candidate.devices[0].interfaces[0].address='192.168.11.1/25'
    with pytest.raises(ValueError,match='/24'):static_routes(candidate)


def test_missing_variant_schema_never_falls_back_to_fixture():
    with pytest.raises(ValueError,match='точной схемы'):CourseRequest(lab=5,variant=4)
    assert recognize_coursework(Path('not-present.pdf'),'5. Настройка статической маршрутизации')==5


def test_traceroutes_reach_three_distinct_destination_networks():
    class Runner:
        def __init__(self):self.destinations=[]
        async def host(self,source,command):
            ip=command.split()[-1];self.destinations.append(ip)
            return ip+' Trace complete'
        def observe(self,*args):pass
    runner=Runner()
    assert asyncio.run(traceroutes(runner,{'a':'192.168.1.2','b':'192.168.2.2','c':'192.168.3.2'},'test'))
    assert len(set(runner.destinations))==3

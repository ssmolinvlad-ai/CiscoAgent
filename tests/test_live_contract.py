"""Opt-in bridge handshake. Never mutates the open Cisco workspace."""
import asyncio
import os
from pathlib import Path

import pytest

from ptagent.bridge import connect_bridge


@pytest.mark.skipif(os.environ.get("PT_LIVE_CONTRACT")!="1",reason="Требуется локально установленный мост")
def test_installed_bridge_discovery_and_schema(tmp_path):
    async def run():
        async with connect_bridge(tmp_path,lambda text:None) as bridge:
            bridge.validate("pt_add_device",{"name":"PC1","model":"PC-PT","x":100,"y":100})
            bridge.validate("pt_add_link",{"device1":"PC1","port1":"FastEthernet0","device2":"SW1","port2":"FastEthernet0/1","cable_type":"straight"})
            bridge.validate("pt_host_ip_config",{"device":"PC1","interface":"FastEthernet0","mode":"static","ip":"192.168.1.10","mask":"255.255.255.0","gateway":"","dns":""})
            bridge.validate("pt_save_project",{"filename":"lab.pkt","directory":str(tmp_path),"overwrite":False})
            catalog=await bridge.call("pt_list_devices")
            assert "PC-PT" in catalog
    asyncio.run(run())

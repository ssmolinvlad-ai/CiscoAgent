"""Headless planning/diagnostics. Live desktop execution stays in the GUI worker."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .bridge import connect_bridge, unpack_result
from .documents import load_assignment
from .environment import inspect_environment
from .planner import analyze


def main():
    parser=argparse.ArgumentParser(description="Packet Tracer Agent: диагностика и подготовка плана")
    parser.add_argument("--diagnose",action="store_true")
    parser.add_argument("--bridge",action="store_true",help="Только чтение состояния моста")
    parser.add_argument("--plan",type=Path)
    parser.add_argument("--ai",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.diagnose:
        (args.output/"environment.json").write_text(json.dumps(inspect_environment(),ensure_ascii=False,indent=2),encoding="utf-8")
    if args.plan:
        plan=asyncio.run(analyze(load_assignment(args.plan),args.ai))
        (args.output/"plan.json").write_text(plan.model_dump_json(indent=2),encoding="utf-8")
    if args.bridge:
        async def probe():
            async with connect_bridge(args.output,lambda text:None) as bridge:
                # A status probe records DISCONNECTED too, without disguising it as connected.
                result=await bridge.session.call_tool("pt_bridge_status",{})
                text=unpack_result(result)
                (args.output/"bridge-status.txt").write_text(text,encoding="utf-8")
                (args.output/"bridge-tools.json").write_text(json.dumps(bridge.tools,ensure_ascii=False,indent=2),encoding="utf-8")
        asyncio.run(probe())


if __name__=="__main__":
    main()

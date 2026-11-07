"""Developer/acceptance entry point for the same executor the GUI uses."""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .bridge import connect_bridge
from .control import RunControl
from .course_runner import CourseRunner
from .coursework import CourseRequest
from .environment import inspect_environment
from .gui import require_open_cisco


async def execute(args):
    desktop=require_open_cisco()
    request=CourseRequest(lab=args.lab,variant=args.variant,hostname=args.hostname,
                          previous_project=args.previous or '',use_current=not args.previous,validation_only=args.validation_only,topology_plan=args.topology_plan or '')
    folder=Path(args.output or (Path.home()/'Documents'/'PacketTracerLabs'/datetime.now().strftime('course-%Y%m%d-%H%M%S'))).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    async with connect_bridge(folder,print) as bridge:
        result=await CourseRunner(bridge,folder,RunControl(),lambda s,p: print(f'{p}% {s}',flush=True),print).run_course(request,inspect_environment(),desktop)
    print('STATUS',result.status,'REPORT',folder/'report.docx',flush=True)
    return 0 if result.status=='completed' else 1


def main():
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser()
    parser.add_argument('--lab',type=int,choices=[2,3,4,5,6,7],required=True)
    parser.add_argument('--topology-plan',help='JSON LabPlan точной схемы варианта для №5')
    parser.add_argument('--variant',type=int,default=1)
    parser.add_argument('--hostname',default='Student-1-Group')
    parser.add_argument('--previous')
    parser.add_argument('--output')
    parser.add_argument('--validation-only',action='store_true')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(execute(args)))


if __name__=='__main__':main()

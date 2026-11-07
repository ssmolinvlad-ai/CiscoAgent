"""Non-blocking simulated PC ping; no host Windows terminal is involved."""
from contextlib import asynccontextmanager
import json

from .bridge import json_reply


def console_script(device, body):
    return ('(function(){try{var d=ipc.network().getDevice('+json.dumps(device)+');'
            'var t=d.getCommandPrompt();'+body+
            "}catch(e){reportResult('ERROR:'+e);}})();")


@asynccontextmanager
async def continuous_ping(runner, device, ip):
    start=console_script(device,'t.enterCommand('+json.dumps('ping -t '+ip)+');reportResult(JSON.stringify({started:true}));')
    stop=console_script(device,"t.enterChar(3,0);reportResult(JSON.stringify({output:String(t.getOutput()),prompt:String(t.getPrompt())}));")
    state={'started':False,'stopped':False,'output':''}
    data=json_reply(await runner.call('pt_send_raw',js_code=start,wait_result=True))
    state['started']=data.get('started') is True
    try:
        yield state
    finally:
        # Ctrl+C must still run after the pause/stop checkpoint starts refusing work.
        result=json_reply(await runner.bridge.call('pt_send_raw',js_code=stop,wait_result=True))
        state['output']=result.get('output','')
        state['stopped']=state['output'].rstrip().endswith(result.get('prompt','C:\\>'))

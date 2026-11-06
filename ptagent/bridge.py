from __future__ import annotations

import json
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import jsonschema


class BridgeError(RuntimeError):
    pass


def local_bridge_env(base: dict) -> dict:
    """Bypass proxies for simulator loopback only; retain external API routing."""
    env=base.copy()
    entries=[]
    for key in ("NO_PROXY","no_proxy"):
        entries.extend(value.strip() for value in env.get(key,"").split(",") if value.strip())
    entries.extend(("127.0.0.1","localhost","::1"))
    bypass=",".join(dict.fromkeys(entries))
    env["NO_PROXY"]=bypass
    env["no_proxy"]=bypass
    return env


def unpack_result(result) -> str:
    text = "\n".join(block.text for block in result.content if getattr(block,"type",None) == "text")
    if result.isError:
        raise BridgeError(text or "Ошибка MCP")
    return text


def json_reply(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BridgeError("Мост вернул ответ неизвестного формата: " + text[:300]) from exc
    if not isinstance(data, dict):
        raise BridgeError("Ожидался объект JSON")
    return data


def ensure_reply(text: str):
    """Upstream often returns error strings with MCP isError=false."""
    if re.search(r"(?im)(?:^|\n)\s*(?:ERROR\b|PT error\b|Error:|Not saved:|Not opened:|No answer\b|No response\b|Could not\b|Unreadable\b|DUPLICATE:)",text) or re.search(r"(?i)\b(?:not connected|bridge is not|timed? out|timeout while|UNKNOWN COMMAND|WAITING FOR ANSWER)\b", text):
        raise BridgeError(text[:1200])
    try:
        data = json.loads(text)
    except (ValueError,TypeError):
        return
    if isinstance(data,dict) and (data.get("ok") is False or data.get("verified") is False or data.get("error") or data.get("rejected_lines")):
        raise BridgeError(text[:1200])


def parse_topology(text: str) -> dict:
    header = re.search(r"^DEVICES:(\d+)\|LINKS:(\d+)", text)
    if not header:
        raise BridgeError("Не удалось подтвердить текущую топологию: " + text[:300])
    devices, links = {}, []
    for name, model, ports in re.findall(r"(?m)^[ \t]+([^\r\n]+?)\s+\[([^\]]+)\](?:[ \t]+\(([^\r\n]*)\))?[ \t]*$",text):
        devices[name.strip()] = {"model":model, "ports":ports}
    for a, ap, cable, b, bp in re.findall(r"(\S+):(\S+)\s+<--\[([^\]]+)\]-->\s+(\S+):(\S+)",text):
        links.append({"a":(a,ap),"b":(b,bp),"cable":cable})
    if len(devices) != int(header[1]) or len(links) != int(header[2]):
        raise BridgeError("Топология содержит неподдерживаемые/скрытые устройства или связи; автоматическое изменение остановлено")
    # Cisco creates this off-canvas power object automatically on the first device.
    # Validate the full inventory first and exclude only this exact unlinked object.
    system_name="Power Distribution Device0"
    if devices.get(system_name,{}).get("model")=="Power Distribution Device":
        if not any(system_name in (link["a"][0],link["b"][0]) for link in links):
            del devices[system_name]
    return {"devices":devices,"links":links}


class MCPBridge:
    def __init__(self, session, tools: dict, log):
        self.session, self.tools, self.log = session, tools, log

    def validate(self, name: str, arguments: dict):
        if name not in self.tools:
            raise BridgeError(f"Мост не поддерживает {name}; требуется совместимая версия")
        try:
            jsonschema.validate(arguments,self.tools[name])
        except jsonschema.ValidationError as exc:
            raise BridgeError(f"Параметры {name} несовместимы с мостом: {exc.message}") from exc

    async def call(self, tool_name: str, **arguments) -> str:
        self.validate(tool_name,arguments)
        self.log(f"Cisco: {tool_name}")
        try:
            result = await self.session.call_tool(tool_name,arguments,read_timeout_seconds=timedelta(seconds=180))
        except Exception as exc:
            raise BridgeError(f"Нет подтверждённого ответа {tool_name}. Действие могло выполниться; автоматический повтор запрещён.") from exc
        text = unpack_result(result)
        ensure_reply(text)
        return text


@asynccontextmanager
async def connect_bridge(run_dir: Path, log, executable: str | None = None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = executable or os.environ.get("PT_BRIDGE_PYTHON")
    args=["-m","packet_tracer_mcp","--stdio"]
    if not command:
        if getattr(sys,"frozen",False):
            bundled = Path(sys.executable).parent.parent / "PacketTracerBridge" / "PacketTracerBridge.exe"
            candidate = Path(sys.executable).parent.parent.parent / ".venv" / "Scripts" / "python.exe"
            if bundled.is_file():
                command=str(bundled)
                args=["--stdio"]
            elif candidate.exists():
                command=str(candidate)
            else:
                raise BridgeError("Не найдена среда моста. Выполните Prepare.cmd или задайте PT_BRIDGE_PYTHON")
        else:
            command = sys.executable
    env = local_bridge_env(os.environ)
    env["PT_MCP_WORKDIR"] = str(run_dir.resolve())
    env["PYTHONIOENCODING"] = "utf-8"
    params = StdioServerParameters(command=command,args=args,env=env,cwd=str(run_dir))
    with (run_dir / "bridge-stderr.log").open("w",encoding="utf-8") as errlog:
        async with stdio_client(params,errlog=errlog) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=180)) as session:
                await session.initialize()
                tools, cursor = {}, None
                while True:
                    result = await session.list_tools(cursor=cursor)
                    tools.update({t.name:t.inputSchema for t in result.tools})
                    cursor = result.nextCursor
                    if not cursor:
                        break
                log(f"Мост запущен, доступно операций: {len(tools)}")
                yield MCPBridge(session,tools,log)

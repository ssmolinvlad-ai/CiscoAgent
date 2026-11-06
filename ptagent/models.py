from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class Stage(StrEnum):
    ANALYZE = "Анализ задания"
    BUILD = "Построение топологии"
    CONNECT = "Соединение устройств"
    CONFIGURE = "Настройка оборудования"
    VERIFY = "Проверка сети"
    REPAIR = "Исправление ошибок"
    SAVE = "Сохранение проекта"
    DONE = "Завершено"


class Interface(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    address: str | None = None
    vlan: int | None = Field(default=None, ge=1, le=4094)

    @field_validator("address")
    @classmethod
    def ipv4(cls, value):
        if value is not None:
            return str(ipaddress.IPv4Interface(value))
        return value


class Device(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
    model: str = Field(min_length=1, max_length=80)
    kind: Literal["router", "switch", "pc", "server", "other"]
    x: int = Field(default=100, ge=0, le=10000)
    y: int = Field(default=100, ge=0, le=10000)
    interfaces: list[Interface] = Field(default_factory=list)
    gateway: str | None = None
    dns: str | None = None
    dhcp: bool = False
    vlans: dict[int, str] = Field(default_factory=dict)
    ios: list[str] = Field(default_factory=list)

    @field_validator("gateway", "dns")
    @classmethod
    def ip(cls, value):
        return str(ipaddress.IPv4Address(value)) if value else value

    @field_validator("ios")
    @classmethod
    def commands(cls, values):
        for line in values:
            if not line.strip() or "\n" in line or "\r" in line or len(line) > 500:
                raise ValueError("Одна команда IOS должна занимать одну строку (до 500 символов)")
            if re.match(r"^\s*(reload|erase|delete|format|write\s+erase|copy\s+.*://)", line, re.I):
                raise ValueError(f"Команда не относится к безопасной настройке лабораторной: {line}")
        return values

    @model_validator(mode="after")
    def validate_device(self):
        names = [i.name for i in self.interfaces]
        if len(names) != len(set(names)):
            raise ValueError(f"Повтор интерфейса у {self.name}")
        if self.kind in ("router", "switch") and self.dhcp:
            raise ValueError("dhcp=true обозначает DHCP-клиент хоста; пул маршрутизатора задайте через ios")
        if self.dhcp and any(i.address for i in self.interfaces):
            raise ValueError("Хост не может одновременно иметь статический IP и DHCP")
        for vlan in self.vlans:
            if not 1 <= vlan <= 4094:
                raise ValueError("VLAN вне диапазона 1–4094")
        return self


class Endpoint(StrictModel):
    device: str
    port: str


class Link(StrictModel):
    a: Endpoint
    b: Endpoint
    cable: str = "straight"

    @field_validator("cable")
    @classmethod
    def known_cable(cls,value):
        aliases={"crossover":"cross","straight-through":"straight","rollover":"roll","serial-dce":"serial"}
        value=aliases.get(value.lower(),value.lower())
        if value not in {"straight","cross","roll","serial","fiber","console","phone","cable","coaxial","auto","wireless","octal","cellular","usb","custom_io"}:
            raise ValueError("Неизвестный тип кабеля")
        return value


class Service(StrictModel):
    device: str
    tool: Literal["pt_server_dns", "pt_server_http", "pt_server_dhcp", "pt_server_service", "pt_configure_dhcp_server"]
    arguments: dict = Field(default_factory=dict)
    description: str


class Check(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    description: str
    kind: Literal["ping", "cli_contains", "host_contains", "host_ip", "http_contains", "tool_contains"]
    device: str
    target: str = ""
    command: str = ""
    contains: str = ""
    expected: bool = True
    tool: str = ""
    arguments: dict = Field(default_factory=dict)
    repair_device: str | None = None
    requirement_ids: list[int] = Field(default_factory=list)


class LabPlan(StrictModel):
    title: str = "Лабораторная работа"
    filename: str = "laboratory.pkt"
    devices: list[Device] = Field(min_length=1, max_length=150)
    links: list[Link] = Field(default_factory=list, max_length=400)
    services: list[Service] = Field(default_factory=list)
    checks: list[Check] = Field(min_length=1, max_length=500)
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        if not re.fullmatch(r"[\w ()-]{1,100}\.pkt", value, re.UNICODE) or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)), *(f"LPT{i}" for i in range(1,10))}:
            raise ValueError("Требуется имя .pkt без пути и специальных символов")
        return value

    @model_validator(mode="after")
    def validate_network(self):
        devices = {d.name: d for d in self.devices}
        if len(devices) != len(self.devices):
            raise ValueError("Имена устройств должны быть уникальными")
        occupied = set()
        ips = set()
        for dev in self.devices:
            for interface in dev.interfaces:
                if interface.address:
                    address = ipaddress.IPv4Interface(interface.address)
                    if address.ip in ips:
                        raise ValueError(f"Дублирующийся IP: {address.ip}")
                    if address.network.prefixlen < 31 and address.ip in (address.network.network_address, address.network.broadcast_address):
                        raise ValueError(f"Адрес сети/broadcast назначен устройству: {address}")
                    ips.add(address.ip)
            if dev.gateway and dev.interfaces and dev.kind in ("pc", "server"):
                nets = [ipaddress.IPv4Interface(i.address).network for i in dev.interfaces if i.address]
                if nets and not any(ipaddress.IPv4Address(dev.gateway) in n for n in nets):
                    raise ValueError(f"Шлюз {dev.name} находится вне подсети")
        for link in self.links:
            if link.a.device == link.b.device:
                raise ValueError("Соединение устройства с самим собой")
            for endpoint in (link.a, link.b):
                if endpoint.device not in devices:
                    raise ValueError(f"Неизвестное устройство: {endpoint.device}")
                key = (endpoint.device, endpoint.port)
                if key in occupied:
                    raise ValueError(f"Порт занят дважды: {key}")
                occupied.add(key)
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("Повтор идентификаторов проверок")
        for obj in [*self.checks, *self.services]:
            if obj.device not in devices:
                raise ValueError(f"Неизвестное устройство в проверке/службе: {obj.device}")
        for check in self.checks:
            if check.repair_device and check.repair_device not in devices:
                raise ValueError("Неизвестное устройство для исправления")
            if check.kind == "ping":
                ipaddress.IPv4Address(check.target)
            if check.kind in ("cli_contains", "host_contains", "http_contains", "tool_contains") and not check.contains:
                raise ValueError("Для проверки содержимого требуется contains")
            if check.kind == "cli_contains" and not re.match(r"^show\s+", check.command, re.I):
                raise ValueError("Проверки CLI допускают только show-команды")
            if check.kind == "host_contains" and not re.fullmatch(r"(?:ipconfig(?: /all)?|nslookup [A-Za-z0-9.-]+|tracert [A-Za-z0-9.-]+)",check.command,re.I):
                raise ValueError("Проверки хоста допускают ipconfig, nslookup и tracert")
            if any(i < 0 or i >= len(self.requirements) for i in check.requirement_ids):
                raise ValueError("Проверка ссылается на несуществующее требование")
        if self.requirements:
            covered = {i for c in self.checks for i in c.requirement_ids}
            if covered != set(range(len(self.requirements))):
                raise ValueError("Для каждого требования нужна связанная проверка (requirement_ids: индексы с нуля)")
        return self


class CheckResult(StrictModel):
    id: str
    description: str
    status: Literal["passed", "failed", "unverified"]
    evidence: str
    attempt: int = 1


class RunReport(StrictModel):
    title: str
    status: Literal["running", "completed", "failed", "stopped", "planned"] = "running"
    started_at: str
    finished_at: str | None = None
    project: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    environment: dict = Field(default_factory=dict)

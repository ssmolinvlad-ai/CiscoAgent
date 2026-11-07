"""Versioned coursework requirements and offline recognition; no external AI required."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel

TITLES = {2: 'Исследование ARP', 3: 'Настройка коммутаторов и VLAN',
          4: 'STP и EtherChannel', 5: 'Статическая маршрутизация', 6: 'Динамическая маршрутизация RIP',
          7: 'Динамическая маршрутизация OSPF'}
PDF_HASHES = {
    '4b1bde22b30d7deb95f282b29b4522fadb79d0d10c5a37d185e9db3e7ddf9017': 5,
    '4b3dafc07c75e10e6d745e43436d3607710e7f853dff84f080007e75c9810034': 2,
    'bd7184995b84607dc094c47c8917d5cf63e2b4c4a2600160b4c438847081be7f': 3,
    '9411e8923351737f3e9e2c33c6a3eb80d6631cac420a2271306994454f3d4893': 4,
    'd49450705aa98ce1ccfb6ef96ac22675d5d6a8a167cb50f5682dc1df378f7d26': 6,
    'd87161f3d11513bc60beeb3528503c8dd65e513e5a48bc623613810240670812': 7,
}


def recognize_coursework(path: Path, text: str) -> int | None:
    # Exact document fingerprints also handle PDFs with broken ToUnicode maps.
    if path.is_file() and path.suffix.lower() == '.pdf':
        known = PDF_HASHES.get(hashlib.sha256(path.read_bytes()).hexdigest())
        if known:
            return known
    normalized = ' '.join(text.lower().split())
    markers = {2: ('изучение протокола arp',),
               3: ('основы настройки управляемых коммутаторов',),
               4: ('построение отказоустойчивых сетей',),
               5: ('настройка статической маршрутизации',),
               6: ('протокол динамической маршрутизации rip',),
               7: ('протокол динамической маршрутизации ospf',)}
    return next((n for n, phrases in markers.items() if any(p in normalized[:600] for p in phrases)), None)


class CourseRequest(StrictModel):
    lab: Literal[2, 3, 4, 5, 6, 7]
    variant: int = Field(default=1, ge=1, le=20)
    hostname: str = Field(default='Student-1-Group', pattern=r'^[A-Za-z][A-Za-z0-9-]{0,62}$')
    previous_project: str = ''
    use_current: bool = True
    physical_windows: bool = False
    physical_interface: int = Field(default=0, ge=0)
    validation_only: bool = False
    topology_plan: str = ''
    console_password: str = Field(default='', repr=False)
    enable_password: str = Field(default='', repr=False)

    @model_validator(mode='after')
    def parameters(self):
        if self.lab==5 and (not self.topology_plan or Path(self.topology_plan).suffix.lower()!='.json'):
            raise ValueError('Для №5 нужен JSON-план точной схемы варианта: в PDF схемы вынесены во внешние ссылки. Подставная топология не используется.')
        for secret in (self.console_password, self.enable_password):
            if secret and not re.fullmatch(r'[A-Za-z0-9_-]{8,40}', secret):
                raise ValueError('Учебный пароль: 8–40 латинских букв, цифр, _ или -')
        if self.previous_project and Path(self.previous_project).suffix.lower() != '.pkt':
            raise ValueError('Исходная сеть должна быть настоящим файлом .pkt')
        return self


# Each triple gives host counts at SW1/SW2/SW3; facts transcribed from the variants table.
VARIANTS = {
 1:(1,(2,1,0),(0,3,2),(2,1,0),(1,2)), 2:(2,(0,1,2),(1,2,2),(2,0,2),(1,3)),
 3:(3,(2,1,0),(0,1,2),(1,2,1),(2,3)), 4:(4,(1,1,1),(2,2,0),(0,3,2),(1,3)),
 5:(1,(2,0,2),(0,3,2),(1,1,1),(1,3)), 6:(2,(1,2,1),(1,2,2),(2,1,0),(2,3)),
 7:(3,(0,3,2),(0,1,2),(2,2,0),(1,2)), 8:(4,(1,2,2),(2,2,0),(2,0,2),(2,1)),
 9:(1,(2,0,2),(0,3,2),(2,2,0),(2,3)), 10:(2,(1,2,1),(3,1,0),(2,1,0),(1,3)),
 11:(3,(0,1,2),(1,2,2),(1,2,1),(3,1)), 12:(4,(2,1,0),(3,1,0),(1,2,2),(1,2)),
 13:(1,(1,1,1),(0,3,2),(2,0,2),(2,3)), 14:(2,(1,2,1),(1,1,1),(0,1,2),(1,2)),
 15:(3,(2,1,0),(1,2,2),(1,1,1),(1,3)), 16:(4,(0,1,2),(2,0,2),(2,1,0),(2,3)),
 17:(1,(1,2,0),(2,0,3),(0,1,1),(1,3)), 18:(2,(2,0,2),(3,2,0),(1,1,1),(1,2)),
 19:(3,(1,1,1),(2,1,2),(2,0,2),(1,2)), 20:(4,(1,3,0),(2,2,1),(2,1,2),(1,3)),
}

REQUIREMENTS = {
 5: ['Точная схема из подтверждённого плана и один ПК у каждого роутера',
     'Уникальные подсети LAN и WAN с маской /24', 'Фактические адреса и состояние портов show ip interface brief',
     'До настройки маршрутов межсетевые ping не проходят', 'Статические маршруты настроены на всех роутерах',
     'Таблицы show ip route подтверждают статические маршруты', 'После настройки все межсетевые ping проходят',
     'Успешные tracert минимум в три сети'],
 2: ['Продолжить сеть из №1 и проверить адреса', 'Пустой ARP-кэш до первого ping',
     'Реальные события ARP и ICMP с решениями OSI', 'ARP-таблицы всех ПК после обмена',
     'Полносвязная проверка ping между ПК', 'Повторный обмен с заполненным кэшем и сравнение',
     'Очистить ARP-кэш в завершение', 'Опыт с ARP на физическом компьютере'],
 3: ['Console-кабель и настройка через Terminal', 'Имя, VLAN1, пароли console/vty/enable, banner',
     'Удаление Console-кабеля и подключение Ethernet', 'Telnet с переходом в привилегированный режим',
     'Точная топология и число ПК по варианту', 'VLAN без использования VLAN1 для рабочих ПК',
     'Связь внутри каждого VLAN', 'Разрешённая межвлановая маршрутизация',
     'Запрет маршрутизации между остальными VLAN'],
 4: ['Продолжить сеть из №3, добавить резервную линию', 'События STP и установившиеся роли портов',
     'Непрерывный ping во время отказа порта', 'Связь и реальный путь после перестроения STP',
     'Восстановить порт и перенести корень STP', 'Работающий EtherChannel из двух линий',
     'Два одновременных потока по разным членам канала при src-mac',
     'При dst-mac оба потока используют один член канала'],
 6: ['Продолжить сеть из №5 и удалить статические маршруты', 'Настроить RIP на всех сетях',
     'Проверить связность и таблицы маршрутизации всех роутеров', 'Не менее трёх трассировок',
     'Добавить цикл и анонсировать новую сеть', 'Подтвердить сокращение маршрута',
     'Выключить роутер и исследовать связность/таблицы', 'Восстановить питание'],
 7: ['Продолжить №5: заменить связи роутеров на serial с clock rate',
     'Добавить минимум три роутера с ПК, сохранить дерево', 'Настроить адреса и OSPF',
     'Соседи и таблицы маршрутов каждого роутера', 'Ping и три трассировки',
     'Разделить дерево на компоненты минимум по два роутера',
     'Подтвердить связь внутри и отсутствие связи между компонентами',
     'Восстановить линии, добавить резервные serial-связи',
     'Изменением bandwidth подтвердить выбор другого маршрута',
     'Повторить отказ и подтвердить сохранение связности'],
}

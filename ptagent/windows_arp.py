"""Opt-in physical Windows exercise, separate from the simulator backend."""
import json
import os
from pathlib import Path
import subprocess
import shutil


def run_windows_arp(folder: Path, interface_index: int = 0):
    if os.name != 'nt':
        raise RuntimeError('Физическая часть требует Windows')
    script=Path(__file__).parent/'assets'/'windows_arp.ps1'
    executable=shutil.which('pwsh.exe') or 'powershell.exe'
    result=subprocess.run([executable,'-NoProfile','-NonInteractive','-File',str(script),
        '-OutputDirectory',str(folder.resolve()),'-InterfaceIndex',str(interface_index)],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=50,
        creationflags=subprocess.CREATE_NO_WINDOW)
    path=folder/'windows-arp.json'
    if not path.is_file():
        raise RuntimeError('Windows-опыт не выполнен. Требуется запуск приложения с правами администратора. '+result.stderr[-500:])
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if result.returncode or not data.get('restored'):
        raise RuntimeError('Windows-опыт не завершён или восстановление не подтверждено: '+str(data))
    return data

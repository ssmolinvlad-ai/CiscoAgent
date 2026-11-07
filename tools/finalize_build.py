"""Keep the newer Qt runtime app-local; never modify Windows system DLLs."""
from pathlib import Path
import shutil
import sys

root=Path(__file__).resolve().parents[1]
application=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else root/"bin"/"PacketTracerAgent"
if not application.resolve().is_relative_to((root/"bin").resolve()):
    raise SystemExit("Папка сборки должна находиться внутри bin приложения")
folder=application/"_internal"
qt=folder/"PySide6"
if not qt.is_dir():
    raise SystemExit("Сначала соберите PacketTracerAgent")
for path in qt.glob("*140*.dll"):
    shutil.copy2(path,folder/path.name)
# Qt on Windows 11 imports the system ICU shim (ucnv_open without a suffix).
# PyInstaller can incorrectly resolve it from an unrelated Poppler directory
# on PATH, whose ICU exports are versioned (_78). Do not bundle that DLL.
for name in ("icuuc.dll","icudt78.dll"):
    unwanted=folder/name
    if unwanted.is_file():
        unwanted.unlink()
print("Локальные библиотеки Visual C++ согласованы с Qt")
if len(sys.argv)==1:
    shutil.copytree(application,root/"bin"/"Current",dirs_exist_ok=True)
    shutil.copytree(application,root/"bin"/"Direct"/"PacketTracerAgent",dirs_exist_ok=True)
    shutil.copytree(application,root/"bin"/"Verified"/"PacketTracerAgent",dirs_exist_ok=True)
    backend=root/"bin"/"PacketTracerBridge"
    if backend.is_dir():
        shutil.copytree(backend,root/"bin"/"Direct"/"PacketTracerBridge",dirs_exist_ok=True)
        shutil.copytree(backend,root/"bin"/"Verified"/"PacketTracerBridge",dirs_exist_ok=True)

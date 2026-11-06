from __future__ import annotations

import os
import time
from pathlib import Path


class DesktopError(RuntimeError):
    pass


class WindowsDesktop:
    """UIA first; vision fallback only for an actual high-confidence screenshot match."""
    def __init__(self, exe: Path):
        self.exe = exe
        self.pid = None

    def _windows(self):
        import psutil
        from pywinauto import Desktop
        pids = []
        for proc in psutil.process_iter(["pid","exe"]):
            try:
                if proc.info["exe"] and Path(proc.info["exe"]).resolve() == self.exe.resolve():
                    pids.append(proc.pid)
            except (OSError,psutil.Error):
                pass
        return [w for w in Desktop(backend="uia").windows() if w.process_id() in pids and w.window_text().startswith("Cisco Packet Tracer")]

    def require_running(self):
        """Attach only. Starting Packet Tracer is deliberately unsupported."""
        if os.name != "nt":
            raise DesktopError("Управление Packet Tracer доступно только в Windows")
        import psutil
        for proc in psutil.process_iter(["pid","exe"]):
            try:
                if proc.info["exe"] and Path(proc.info["exe"]).resolve()==self.exe.resolve():
                    self.pid=proc.pid
                    return self.pid
            except (OSError,psutil.Error):
                continue
        self.pid=None
        raise DesktopError("Cisco Packet Tracer закрыт. Откройте Cisco самостоятельно и повторите действие. Программа не запускает Cisco.")

    def new_project(self):
        windows = self._windows()
        if len(windows) != 1:
            raise DesktopError("Окно Cisco изменилось")
        window = windows[0]
        buttons = [b for b in window.descendants(control_type="Button") if b.window_text() in ("New","Создать")]
        if len(buttons) != 1:
            raise DesktopError("Не найдена однозначная кнопка New; текущий проект сохранён, очистка не выполнена")
        buttons[0].invoke()
        # Backup was already verified by the executor. Never accept a discard dialog here.
        time.sleep(.7)
        dialogs = [w for w in self._windows() if w.handle != window.handle]
        if dialogs:
            raise DesktopError("Cisco показал дополнительный диалог; автоматическое удаление изменений не выполняется")

    def click_template(self, template: Path, threshold=.96):
        import cv2
        import numpy as np
        import pyautogui
        windows = self._windows()
        if len(windows) != 1:
            raise DesktopError("Нет единственного окна Cisco")
        window = windows[0]
        window.set_focus()
        rect = window.rectangle()
        image = np.array(pyautogui.screenshot(region=(rect.left,rect.top,rect.width(),rect.height())))
        needle = cv2.imdecode(np.frombuffer(template.read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
        if needle is None or needle.shape[0]>image.shape[0] or needle.shape[1]>image.shape[1]:
            raise DesktopError("Некорректный шаблон элемента")
        result = cv2.matchTemplate(cv2.cvtColor(image,cv2.COLOR_RGB2BGR),needle,cv2.TM_CCOEFF_NORMED)
        _, confidence, _, point = cv2.minMaxLoc(result)
        if confidence < threshold:
            raise DesktopError(f"Элемент не распознан с достаточной уверенностью: {confidence:.2f}")
        # Refuse multiple separated matches; DPI/theme differences require fresh templates.
        ys,xs = np.where(result>=threshold)
        if any(abs(int(x)-point[0])>needle.shape[1]//2 or abs(int(y)-point[1])>needle.shape[0]//2 for x,y in zip(xs,ys)):
            raise DesktopError("Несколько совпадений элемента")
        pyautogui.FAILSAFE = True
        pyautogui.click(rect.left+point[0]+needle.shape[1]//2,rect.top+point[1]+needle.shape[0]//2)

    def capture(self,path:Path):
        windows = self._windows()
        if len(windows)!=1:
            raise DesktopError("Не удалось определить окно для снимка")
        windows[0].capture_as_image().save(path)

"""Terminal presentation for the operator wizard. All prompts remain line-based."""
from contextlib import contextmanager
import os
import shutil
import sys
import threading
import textwrap
import time


class WizardError(Exception):
    """A message safe to show to the operator."""


class Terminal:
    def __init__(self):
        self.plain = False
        self.step = ""
        self.pending = None

    def rich(self, stream):
        return (not self.plain and os.environ.get("ANET_PLAIN") != "1"
                and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
                and stream.isatty())

    def accent(self, text):
        return f"\033[1;36m{text}\033[0m" if self.rich(sys.stdout) else text

    def title(self):
        title = "ANet  /  Установка VPN"
        description = "Входы, выходы и панель на ваших VPS."
        width = min(66, shutil.get_terminal_size().columns)
        if self.rich(sys.stdout) and width >= 16:
            print(self.accent("╭" + "─" * (width - 2) + "╮"))
            for content, highlight in ((title, True), (description, False)):
                for line in textwrap.wrap(content, width=width - 6):
                    inside = line.ljust(width - 6)
                    print(self.accent("│") + "  " + (self.accent(inside) if highlight else inside) + "  " + self.accent("│"))
            print(self.accent("╰" + "─" * (width - 2) + "╯"))
        else:
            print(title)
            print(description)
        print()

    def guide(self):
        print("\n" + self.accent("Схема и этапы установки"))
        print("Клиент → входной VPS → по ANet на выходной VPS → интернет.")
        print("Клиент подключается по WireGuard, VLESS или ANet. Панель будет на текущем VPS.")
        print("\nЧто подготовить")
        print("  • Две VPS с Debian или Ubuntu на amd64, systemd и /dev/net/tun.")
        print("  • Адреса, SSH-порты и доступ по ключу или паролю. Для установки пакетов нужен root или sudo.")
        print("  • Комплект образов поставки. Для VLESS с Let’s Encrypt — домен, направленный на входной VPS, и открытые TCP 80/443. Сертификат выпустит мастер.")
        print("\nШесть этапов")
        for i, stage in enumerate(("Поставка и топология", "Проверка плана", "Проверка доступа к VPS",
                                   "Подготовка узлов и доставка образов", "Запуск VPN и проверка маршрутов",
                                   "Панель и клиентские профили"), 1):
            print(f"  {i}. {stage}")
        input("\nНажмите Enter, чтобы вернуться в главное меню. ")
        print()

    def stage(self, number, name):
        self.step = f"Этап {number}/6 · {name}"
        print("\n" + self.accent(self.step), flush=True)

    def menu(self, title, options, default):
        print(title)
        for value, label, detail in options:
            selected = value == default and self.rich(sys.stdout)
            heading = f"› {value}   {label}" if selected else f"  {value}   {label}"
            heading = self.accent(heading) if selected else heading
            if detail and self.rich(sys.stdout) and shutil.get_terminal_size().columns < 48:
                print(heading)
                print(textwrap.fill(detail, width=max(10, shutil.get_terminal_size().columns),
                                    initial_indent="      ", subsequent_indent="      "))
            else:
                print(heading + (f" — {detail}" if detail else ""))
        while True:
            value = input(f"Выбор [{default}] › ").strip() or str(default)
            if value in {str(i) for i, _, _ in options}:
                return int(value)
            print("Введите номер одного из пунктов.")

    @contextmanager
    def activity(self, label, *, interactive=False, progress=None):
        # Only animate operations with captured output. ssh/sudo own the
        # terminal from start to finish whenever they can prompt for a secret.
        stream = sys.stderr
        start = time.monotonic()
        stop = threading.Event()
        thread = None

        def elapsed():
            seconds = int(time.monotonic() - start)
            return f"{seconds // 60:02d}:{seconds % 60:02d}"

        def draw():
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            frame = 0
            while True:
                percent = f" {progress():3.0%}" if progress else ""
                width = max(1, shutil.get_terminal_size().columns - 10 - len(percent))
                print(f"\r\033[2K{frames[frame % len(frames)]} {label[:width]}{percent} {elapsed()}",
                      end="", file=stream, flush=True)
                if stop.wait(0.16):
                    return
                frame += 1

        if self.rich(stream) and not interactive:
            thread = threading.Thread(target=draw, daemon=True)
            thread.start()
        else:
            print(f"  · {label}", file=stream, flush=True)
        result = "Прервано"
        try:
            yield
            result = "Готово"
        finally:
            if thread:
                stop.set()
                thread.join()
                print("\r\033[2K", end="", file=stream)
            print(f"  {result} · {label} · {elapsed()}", file=stream, flush=True)

    def error(self, reason):
        where = f" ({self.step})" if self.step else ""
        print(f"\nУстановка остановлена{where}. {reason}", file=sys.stderr)
        if self.pending and self.pending.is_file():
            print("План сохранён. Устраните причину, повторите команду установки и выберите «Продолжить».",
                  file=sys.stderr)


def failure_hint(output, fallback):
    # Docker or SSH diagnostics can include sensitive paths, environment or
    # profile contents. Display only recognized causes and a fixed next action.
    data = output.lower()
    causes = [
        (b"vless tls handshake failed", "Проверьте домен VLESS, срок действия, цепочку и сертификат на VPN-порту."),
        (b"no space left", "Освободите место для архива и образов Docker."),
        (b"permission denied", "Проверьте права пользователя SSH на каталог установки и доступ к Docker."),
        (b"address already in use", "Порт уже занят. Освободите его или выберите другие порты в расширенных настройках."),
        (b"port is already allocated", "Порт уже занят. Освободите его или выберите другие порты в расширенных настройках."),
        (b"sha-256 mismatch", "Контрольная сумма образов отличается от манифеста. Восстановите комплект из поставки."),
        (b"no such image", "В Docker отсутствует нужный образ. Проверьте состав комплекта."),
        (b"connection refused", "Проверьте адрес и порт SSH, сетевой доступ и службу на VPS."),
    ]
    return next((hint for fragment, hint in causes if fragment in data), fallback)

"""Читання й запис секретів у зв'язці ключів macOS (команда security). У файли секрети не пишемо."""
import getpass
import subprocess

from .config import KEYCHAIN


def get(name: str) -> str | None:
    r = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
         "-s", KEYCHAIN[name], "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def put(name: str, value: str) -> None:
    # Команда йде через stdin (security -i), щоб секрет не було видно у списку процесів.
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    cmd = (f"add-generic-password -U -a {q(getpass.getuser())} "
           f"-s {q(KEYCHAIN[name])} -w {q(value)}\n")
    r = subprocess.run(["/usr/bin/security", "-i"], input=cmd, text=True, capture_output=True)
    if r.returncode != 0 or get(name) != value:
        raise RuntimeError(f"Не вдалося зберегти «{KEYCHAIN[name]}» у зв'язку ключів")


def need(name: str) -> str:
    v = get(name)
    if not v:
        raise SystemExit(f"Немає секрету «{KEYCHAIN[name]}» у зв'язці ключів. Запустіть налаштування: tbilisi-agent-setup")
    return v

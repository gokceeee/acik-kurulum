"""
ACIK Kurulum uygulamasinin en dis giris noktasi.

Stajyer Notu:
- Bu dosya "uygulamayi ac" dugmesi gibi dusunulebilir.
- Burada is kurallari yok; sadece dogru klasoru bulma, admin yetkisi kontrolu
  ve Qt uygulamasini baslatma karari var.
- EXE olarak paketlendikten sonra ilk calisan yer burasidir.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

APP_VERSION = "5.21.31"


def write_bootstrap_log(message: str) -> None:
    """Best-effort diagnostics for startup and post-login launch issues.

    This never interrupts onboarding if a standard user cannot write under
    ProgramData; the user-specific post-login log remains the fallback.
    """
    program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
    paths = [program_data / "AcikOnboarding" / "bootstrap-v5.3.log"]
    # Portable runs also keep a small, non-secret diagnostic log beside the EXE.
    # This makes another laptop's startup issue inspectable from the USB drive.
    paths.append(app_root() / "logs" / "bootstrap-v5.3.log")
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{message}\n")
        except OSError:
            pass


def app_root() -> Path:
    """Calisma kokunu belirler.

    EXE modunda `sys.executable` dosyanin bulundugu klasoru verir.
    Script modunda ise bu dosyanin bulundugu klasor kullanilir.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def relaunch_arguments() -> str:
    """Mevcut komut satiri argumanlarini yeniden kullanmak icin formatlar."""
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline(sys.argv[1:])
    script_path = Path(sys.argv[0]).resolve()
    return subprocess.list2cmdline([str(script_path), *sys.argv[1:]])


ROOT = app_root()
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

def ensure_venv_and_dependencies() -> None:
    if getattr(sys, "frozen", False):
        return

    # Development and release builds intentionally use different environments.
    # A stale .venv copied from another PC must never prevent the source app
    # from starting, and build dependencies must not leak into the runtime.
    venv_dir = ROOT / ".dev-venv"
    venv_python = venv_dir / "Scripts" / "python.exe"

    if Path(sys.executable).resolve() != venv_python.resolve():
        if not venv_python.exists():
            print("Gelistirme ortami (.dev-venv) bulunamadi, olusturuluyor...")
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])

        # Validate the interpreter before relaunching. A partially copied venv
        # can contain python.exe but still point to a missing base interpreter.
        health = subprocess.run(
            [str(venv_python), "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if health.returncode != 0:
            raise RuntimeError(
                "Gelistirme sanal ortami bozuk. .dev-venv klasorunu silip yeniden deneyin."
            )

        print("Gelistirme sanal ortamina gecis yapiliyor...")
        sys.exit(subprocess.call([str(venv_python)] + sys.argv))

    import importlib.util

    if importlib.util.find_spec("PySide6") is None:
        req_file = ROOT / "requirements.txt"
        if req_file.exists():
            print("Gerekli kutuphaneler eksik, otomatik yukleniyor (bu islem biraz surebilir)...")
            try:
                subprocess.check_call([str(venv_python), "-m", "pip", "install", "-r", str(req_file)])
            except subprocess.CalledProcessError:
                print("Kutuphaneler yuklenirken hata olustu. Lutfen internet baglantinizi kontrol edin.")

            sys.exit(subprocess.call([str(venv_python)] + sys.argv))

ensure_venv_and_dependencies()

from acik_onboarding.app import run, run_post_login, run_system_finalize


def is_admin() -> bool:
    """Windows oturumunun yonetici hakki tasiyip tasimadigini kontrol eder."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Gerekirse uygulamayi Windows UAC ile yeniden yonetici olarak acmaya calisir."""
    try:
        params = relaunch_arguments()
        python_exe = sys.executable
        if not getattr(sys, "frozen", False):
            venv_python = ROOT / ".dev-venv" / "Scripts" / "python.exe"
            if venv_python.exists():
                python_exe = str(venv_python)

        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            str(python_exe),
            params,
            str(ROOT),
            1,
        )
        return result > 32
    except Exception:
        return False


_instance_mutexes: list[int] = []


def check_single_instance(mutex_name: str = "Local\\AcikOnboardingSingleInstanceMutex") -> bool:
    if sys.platform != "win32":
        return True
    try:
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, True, mutex_name)
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            if handle:
                kernel32.CloseHandle(handle)
            return False
        _instance_mutexes.append(handle)
        return True
    except Exception:
        return True


def show_fatal_error(message: str) -> None:
    """Qt acilamasa bile kullaniciya en azindan sistem seviyesinde hata gosterir."""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "AÇIK Kurulum", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    """Boot akisini yoneten ana karar noktasi.

    Normal calisma:
    1. Post-login modu mu kontrol et
    2. Gerekirse yonetisi yetkisi iste
    3. Normal UI veya gecikmeli ikinci faz yardimcisini baslat
    """
    post_login_mode = "--post-login" in sys.argv[1:]
    system_finalize_mode = "--system-finalize" in sys.argv[1:]
    recovery_mode = "--recovery" in sys.argv[1:]
    mode = "system-finalize" if system_finalize_mode else ("post-login" if post_login_mode else ("recovery" if recovery_mode else "main"))
    write_bootstrap_log(
        f"START version={APP_VERSION} mode={mode} frozen={getattr(sys, 'frozen', False)} "
        f"admin={is_admin()} root={ROOT}"
    )
    post_login_run_id = ""
    post_login_target = ""
    if post_login_mode:
        try:
            argument_index = sys.argv.index("--post-login")
            post_login_run_id = sys.argv[argument_index + 1]
            post_login_target = sys.argv[argument_index + 2]
        except (ValueError, IndexError):
            write_bootstrap_log("POST_LOGIN_ARGUMENTS_MISSING")
            show_fatal_error("Kullanici fazi icin run_id veya hedef kullanici eksik.")
            return 1
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", post_login_run_id):
            write_bootstrap_log("POST_LOGIN_RUN_ID_INVALID")
            show_fatal_error("Kullanici fazi run_id degeri gecersiz.")
            return 1

    skip_elevation = os.environ.get("ACIK_SKIP_ELEVATION") == "1"
    if post_login_mode:
        skip_elevation = True
    if system_finalize_mode:
        skip_elevation = True
        if not is_admin():
            show_fatal_error("SYSTEM finalizasyonu yonetici yetkisi olmadan calistirilamaz.")
            return 1
    if sys.platform == "win32" and not skip_elevation and not is_admin():
        write_bootstrap_log("REQUESTING_ELEVATION")
        if relaunch_as_admin():
            return 0
        write_bootstrap_log("ELEVATION_REQUEST_FAILED")
        return 1
    # Only the final elevated process owns the mutex. If the unelevated parent
    # acquires it first, the UAC child can mistake that parent for another app
    # instance and exit before the window is shown.
    if not post_login_mode and not system_finalize_mode and not check_single_instance(
        "Local\\AcikOnboardingRecoveryMutex"
        if recovery_mode
        else "Local\\AcikOnboardingSingleInstanceMutex"
    ):
        write_bootstrap_log("MAIN_INSTANCE_ALREADY_RUNNING")
        show_fatal_error(
            "AÇIK Kurulum uygulamasının başka bir kopyası zaten çalışıyor!\n\n"
            "Lütfen açık olan pencereyi kapatıp tekrar deneyin."
        )
        return 1
    if post_login_mode and not check_single_instance(
        f"Local\\AcikOnboardingUserPhase-{post_login_run_id}"
    ):
        write_bootstrap_log(f"POST_LOGIN_INSTANCE_ALREADY_RUNNING run_id={post_login_run_id}")
        return 0
    try:
        if system_finalize_mode:
            try:
                argument_index = sys.argv.index("--system-finalize")
                run_id = sys.argv[argument_index + 1]
            except (ValueError, IndexError):
                write_bootstrap_log("SYSTEM_FINALIZE_ARGUMENTS_MISSING")
                show_fatal_error("SYSTEM finalizasyonu icin run_id eksik.")
                return 1
            write_bootstrap_log(f"ENTER_SYSTEM_FINALIZE run_id={run_id}")
            return run_system_finalize(ROOT, run_id)
        if post_login_mode:
            write_bootstrap_log(f"ENTER_POST_LOGIN run_id={post_login_run_id} target={post_login_target}")
            return run_post_login(ROOT, post_login_run_id, post_login_target)
        write_bootstrap_log("ENTER_MAIN_UI")
        return run(ROOT)
    except Exception as exc:
        write_bootstrap_log(f"STARTUP_ERROR mode={mode} error={exc}")
        show_fatal_error(f"Uygulama başlatılamadı.\n\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
Qt uygulamasini gercekten ayaga kaldiran ince katman.

Stajyer Notu:
- `run_app.py` dis bootstrapping yapar, bu dosya ise "uygulamayi kur ve calistir"
  isini yapar.
- Burada mantik az, baglanti coktur: config yuklenir, service olusur, pencere acilir.
- Post-login fonksiyonu ikinci faz otomasyonu icin ayridir.
"""

from __future__ import annotations

import json
import ctypes
import os
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QTextBrowser,
    QVBoxLayout,
)

from .config import load_app_config
from .services import OnboardingService
from .ui import MainWindow


def run(base_dir: Path) -> int:
    """Ana UI oturumunu olusturur ve event loop'u baslatir."""
    app = QApplication([])
    config = load_app_config(base_dir)
    service = OnboardingService(config)
    window = MainWindow(config, service)
    window.showMaximized()
    return app.exec()


class PostLoginWorker(QThread):
    progress = Signal(str)
    finished_task = Signal(bool, str)

    def __init__(
        self,
        service: OnboardingService,
        logger_func: Callable[[str], None],
        run_id: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.logger_func = logger_func
        self.run_id = run_id

    def run(self) -> None:
        def local_logger(msg: str) -> None:
            self.logger_func(msg)
            self.progress.emit(msg)

        try:
            messages = self.service.handle_deferred_startup(local_logger, self.run_id)
            errors = [text for _title, text, level in messages if level == "error"]
            self.finished_task.emit(not errors, "\n".join(errors))
        except Exception as exc:
            local_logger(f"Post-login gorevi hata ile durdu: {exc}")
            self.finished_task.emit(False, str(exc))


def build_post_login_report_dialog(
    service: OnboardingService,
    run_id: str,
    parent: QDialog | None = None,
) -> QDialog:
    """Show a read-only, auto-refreshing summary after the second phase.

    The SYSTEM reconciliation task can finish a few seconds after the user
    phase.  Keeping this dialog visible makes the result and report explicit
    instead of silently closing the only post-login window.
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle("AÇIK Kurulum | Kurulum Raporu")
    dialog.resize(760, 560)
    dialog.setMinimumSize(560, 420)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 20)
    layout.setSpacing(14)

    title = QLabel("Kurulum Raporu")
    title.setStyleSheet("font-size: 20px; font-weight: bold; color: #d0af68;")
    status = QLabel("Rapor hazırlanıyor…")
    status.setWordWrap(True)
    detail = QTextBrowser()
    detail.setReadOnly(True)
    detail.setPlainText("Sistem finalizasyonu tamamlandığında rapor burada güncellenecek.")
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.close)

    layout.addWidget(title)
    layout.addWidget(status)
    layout.addWidget(detail, 1)
    layout.addWidget(buttons)

    report_path = service.report_output_dir() / f"{run_id}.json"

    def refresh() -> None:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            status.setText("Rapor henüz hazırlanıyor. Lütfen bu pencereyi açık bırakın.")
            return
        except (OSError, json.JSONDecodeError) as exc:
            status.setText(f"Rapor şu an okunamadı: {exc}")
            return

        post_login = report.get("post_login", {})
        if not isinstance(post_login, dict):
            post_login = {}
        report_status = str(post_login.get("status") or report.get("status") or "bekliyor")
        status.setText(f"Rapor durumu: {report_status}")

        lines = ["KURULUM ÖZETİ", ""]
        for label, key in (
            ("Şirket", "company_name"),
            ("Kullanıcı", "username"),
            ("Cihaz", "computer_name"),
            ("Başlangıç", "run_started_at"),
            ("Durum", "status"),
        ):
            value = report.get(key, "")
            if value:
                lines.append(f"{label}: {value}")
        tasks = post_login.get("tasks", {})
        if isinstance(tasks, dict):
            lines.extend(["", "İKİNCİ FAZ ADIMLARI"])
            for name, task in tasks.items():
                if not isinstance(task, dict) or not task.get("enabled"):
                    continue
                task_status = str(task.get("status", "bekliyor"))
                error = str(task.get("error", "")).strip()
                lines.append(f"• {name}: {task_status}" + (f" — {error}" if error else ""))
        detail.setPlainText("\n".join(lines))

    timer = QTimer(dialog)
    timer.timeout.connect(refresh)
    timer.start(2500)
    refresh()
    return dialog


def run_post_login(base_dir: Path, run_id: str, target_username: str) -> int:
    """Run the scheduled user phase without owning or blocking the desktop."""
    current_username = os.environ.get("USERNAME", "").strip()
    if not current_username or current_username.casefold() != target_username.casefold():
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "Bekleyen ikinci faz bu Windows kullanicisi icin calistirilamaz.\n\n"
                f"Hedef: {target_username}\nMevcut: {current_username or 'bilinmiyor'}\n\n"
                "Hedef kullanici ile oturum acin; AÇIK Kurulum otomatik baslayacaktir.",
                "AÇIK Kurulum | Kullanici Kontrolu",
                0x30,
            )
        except Exception:
            pass
        return 0

    config = load_app_config(base_dir, public_only=True)
    service = OnboardingService(config)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    log_path = local_app_data / "AcikOnboarding" / "logs" / f"post-login-{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_log_path = service.user_phase_log_path(run_id)

    def file_logger(message: str) -> None:
        line = message.rstrip() + "\n"
        for destination in (log_path, runtime_log_path):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                # The protected runtime folder may still be waiting for the
                # SYSTEM ACL handoff. The per-user diagnostic log remains.
                continue

    # This is a Scheduled Task, not an interactive setup window.  The former
    # topmost frameless progress dialog could make a manual reboot look like a
    # frozen desktop and kept the operator from opening the main application.
    # Progress remains observable through the durable report and log files.
    file_logger("Post-login kullanici fazi arka planda baslatildi.")
    try:
        messages = service.handle_deferred_startup(file_logger, run_id)
    except Exception as exc:
        file_logger(f"Post-login gorevi hata ile durdu: {exc}")
        service.export_task_failure_to_usb(
            "Post-login kullanici fazi",
            str(exc),
            file_logger,
            run_id=run_id,
        )
        return 1
    errors = [text for _title, text, level in messages if level == "error"]
    if errors:
        error_text = " | ".join(errors)
        file_logger("Post-login kullanici fazi hata ile tamamlandi: " + error_text)
        service.export_task_failure_to_usb(
            "Post-login kullanici fazi",
            error_text,
            file_logger,
            run_id=run_id,
        )
        return 1
    file_logger("Post-login kullanici fazi tamamlandi; SYSTEM finalizer raporu isleyecek.")
    return 0

    # Legacy visual helper kept below temporarily for source compatibility.
    # It is unreachable: scheduled post-login work must not block the desktop.
    app = QApplication(sys.argv)
    dialog = QDialog()
    dialog.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint)
    dialog.resize(640, 230)
    dialog.setMinimumSize(420, 210)
    dialog.setStyleSheet("background-color: #1a1a1a; color: #f4efe7; border: 2px solid #d0af68; border-radius: 8px;")
    
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(30, 30, 30, 30)
    layout.setSpacing(15)
    
    title_label = QLabel("AÇIK Kurulum | İkinci Faz Tamamlanıyor")
    title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #d0af68; border: none;")
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    
    progress_bar = QProgressBar()
    progress_bar.setRange(0, 0) # Indeterminate
    progress_bar.setStyleSheet("QProgressBar { border: 1px solid #333; border-radius: 4px; background: #2b2b2b; } QProgressBar::chunk { background: #d0af68; }")
    progress_bar.setFixedHeight(20)
    
    status_label = QLabel("Lütfen bekleyin...")
    status_label.setStyleSheet("font-size: 13px; color: #aaa; border: none;")
    status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    status_label.setWordWrap(True)
    
    layout.addWidget(title_label)
    layout.addWidget(progress_bar)
    layout.addWidget(status_label)
    
    worker = PostLoginWorker(service, file_logger, run_id, dialog)

    def update_label(msg: str) -> None:
        short_msg = msg if len(msg) < 100 else msg[:97] + "..."
        status_label.setText(f"İşleniyor: {short_msg}")

    worker.progress.connect(update_label)

    report_dialog: QDialog | None = None

    def on_finished(success: bool, error_msg: str) -> None:
        if not success:
            title_label.setText("İkinci faz tamamlanamadı")
            status_label.setText(f"Hata: {error_msg}")
            progress_bar.setRange(0, 1)
            progress_bar.setValue(0)
            delay = 6000
        else:
            title_label.setText("İkinci faz tamamlandı")
            status_label.setText("Rapor açılıyor…")
            progress_bar.setRange(0, 1)
            progress_bar.setValue(1)
            delay = 1200

        def open_report() -> None:
            nonlocal report_dialog
            dialog.close()
            report_dialog = build_post_login_report_dialog(service, run_id)
            report_dialog.finished.connect(app.quit)
            report_dialog.show()

        QTimer.singleShot(delay, open_report)

    worker.finished_task.connect(on_finished)
    file_logger("Post-login penceresi acildi; SYSTEM ACL hazirligi icin kisa gecikme uygulanacak.")
    dialog.show()
    # At domain logon the common Startup helper and the SYSTEM ACL repair can
    # start in either order. Give the SYSTEM task a small head start, then the
    # service continues retrying the protected plan for up to five minutes.
    QTimer.singleShot(8000, worker.start)
    
    return app.exec()


def run_system_finalize(base_dir: Path, run_id: str) -> int:
    """Run privileged post-login work without opening the main UI."""
    config = load_app_config(base_dir)
    service = OnboardingService(config)
    log_path = service.system_runtime_dir() / "system_finalize.log"

    def file_logger(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")

    try:
        messages = service.handle_system_finalize(run_id, file_logger)
    except Exception as exc:
        file_logger(f"SYSTEM finalizasyonu basarisiz: {exc}")
        service.export_task_failure_to_usb(
            "SYSTEM post-login finalizasyonu",
            str(exc),
            file_logger,
            run_id=run_id,
        )
        return 1
    errors = [text for _title, text, level in messages if level == "error"]
    if errors:
        error_text = " | ".join(errors)
        file_logger("SYSTEM finalizasyonu hata ile tamamlandi: " + error_text)
        service.export_task_failure_to_usb(
            "SYSTEM post-login finalizasyonu",
            error_text,
            file_logger,
            run_id=run_id,
        )
        return 1
    return 0

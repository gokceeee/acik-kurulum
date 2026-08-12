"""
ACIK Kurulum'un PySide6 arayuzu.

Stajyer Notu:
- Bu dosya ekranda ne goruldugunu ve kullanici etkileşimlerini yonetir.
- Is kurallari burada tutulmamalidir; agir Windows islemleri `services.py` icine gider.
- UI degisikligi yaparken once burada ilgili widget ve layout'u bulun, sonra
  gerekiyorsa servis ve config tarafini guncelleyin.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QKeySequence, QPixmap, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .config import (
    AppConfig,
    CompanyProfile,
    WorkflowProfile,
    load_app_config,
    save_app_config,
    validate_app_config,
)
from .services import OnboardingRequest, OnboardingService


OptionRow = tuple[str, str, str, bool]
OptionGroupDefinition = tuple[str, str, list[OptionRow]]
SETTINGS_PASSWORD_HASH = "a65c27b21c9f256c49d4c72bb776f1fd66bc84b85d753d3d0576b8e152a93c1b"


def repair_display_text(value: str) -> str:
    """Repair legacy UTF-8-as-Windows-1252 text without touching valid Turkish.

    Some old portable config files were saved through a legacy Windows code
    page.  Keep the repair at the display boundary so normal UTF-8 source and
    operational values (such as company keys) are never mutated.
    """
    repaired = value
    for _ in range(3):
        if not any(marker in repaired for marker in ("Ã", "Ä", "Å", "â", "�")):
            break
        try:
            candidate = repaired.encode("cp1252").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            break
        if candidate == repaired:
            break
        repaired = candidate
    return repaired


def repair_widget_texts(root: QWidget) -> None:
    """Normalize visible static text after a widget tree has been built."""
    for widget_type in (QLabel, QPushButton, QCheckBox, QToolButton, QGroupBox):
        for widget in root.findChildren(widget_type):
            text = widget.text() if hasattr(widget, "text") else ""
            if text:
                widget.setText(repair_display_text(text))
            if widget.toolTip():
                widget.setToolTip(repair_display_text(widget.toolTip()))
            if widget.accessibleName():
                widget.setAccessibleName(repair_display_text(widget.accessibleName()))
    for table in root.findChildren(QTableWidget):
        for column in range(table.columnCount()):
            header = table.horizontalHeaderItem(column)
            if header is not None:
                header.setText(repair_display_text(header.text()))
    for tabs in root.findChildren(QTabWidget):
        for index in range(tabs.count()):
            tabs.setTabText(index, repair_display_text(tabs.tabText(index)))

OPTION_GROUPS: list[OptionGroupDefinition] = [
    # Stajyer Notu:
    # Bu liste ayni anda 3 yeri besler:
    # 1. Ana ekrandaki kurulum adim kartlari
    # 2. Profil tanimlarindaki acik/kapali varsayimlari
    # 3. Servise giden option anahtarlari
    #
    # Yeni bir toggle eklerken sadece buton cizmek yetmez; bu yapinin ilgili
    # servis akisi ve config profilleriyle birlikte dusunulmesi gerekir.
    (
        "Hesap ve Yetki",
        "Kullanıcı oluşturma ve yetki adımları.",
        [
            ("rename_admin", "Lokaladm", "Yerleşik yönetici hesabını lokaladm olarak hazırlar.", True),
            ("ip_admin", "IP Admin", "Kullanıcıya ağ ayarlarını değiştirme yetkisi verir.", False),
            ("administrator", "Administrator", "Kullanıcıyı yerel Administrators grubuna ekler.", False),
            ("delete_x_user", "x Kullanıcısını Sil", "Tüm adımlardan sonra x oturumunu kapatır, hesabı/profili doğrular ve en son yeniden başlatır.", True),
        ],
    ),
    (
        "Ağ ve Erişim",
        "Yeni kullanıcı oturumunda tamamlanacak erişim adımları.",
        [
            ("wifi_sync", "Wi-Fi + Saat Eşitle", "Kayıtlı ağa bağlanır ve sistem saatini eşitler.", True),
            ("main_file_server", "Ana File Server", "File server bağlantısını hazırlar ve masaüstüne kısayol bırakır.", False),
            ("network_printer", "Ağ Yazıcısı (Sadece Domain)", "acik_printer bağlantısını yeni kullanıcı için kurmayı dener.", False),
        ],
    ),
    (
        "Son Kullanıcı",
        "Yeni oturum açıldıktan sonra tamamlanacak masaüstü ve uygulama hazırlıkları.",
        [
            ("desktop_wallpaper", "Sabit Arka Plan", "Yalnızca oluşturulan yerel standart kullanıcıya wallpaper.jpg uygular ve değişikliği kilitler. Domain veya Administrator kullanıcıya uygulanmaz.", False),
            ("desktop_signature", "Masaüstü İmza", "Şirket imza dosyalarını yeni kullanıcının masaüstüne kopyalar.", False),
            ("classic_outlook", "Outlook Classic", "Uygulamayı açar; hesap girişi kullanıcı ve MFA akışıyla tamamlanır.", False),
        ],
    ),
    (
        "Kurulum ve Sistem",
        "Uygulama ve sistem sonlandırma adımları.",
        [
            ("anydesk", "AnyDesk", "AnyDesk uygulamasını sessiz modda kurar ve kurulumu doğrular.", False),
            ("eset", "ESET", "Kurulum sonunda ESET yükleyicisini doğrudan başlatır.", True),
            ("windows_update", "Win Update", "Windows Update ekranını açar ve kullanıcıya bırakır.", False),
            ("windows_activation", "Win Etkinleştir", "Tanımlı ürün anahtarıyla etkinleştirmeyi dener.", True),
            ("lock_screen", "Kilit Ekranı İlkesi", "Kurumsal kilit ekranı görselini cihaz genelinde uygular (Enterprise/Education Group Policy, Pro'da desteklenen Personalization CSP). Yalnızca bilinçli olarak seçildiğinde çalışır; yönetilmeyen Windows Pro cihazlarda desteklenmez.", False),
            ("hackbgrt", "HackBGRT", "EFI/önyükleme kaydını değiştirir; yalnızca bilinçli olarak seçildiğinde çalıştırılır.", False),
            ("restart", "Yeniden Başlat", "Tüm adımlar bittikten sonra bilgisayarı yeniden başlatır.", True),
        ],
    ),
]

OPTION_LABELS = {
    option_name: label
    for _, _, options in OPTION_GROUPS
    for option_name, label, _, _ in options
}


class LiveLogHighlighter(QSyntaxHighlighter):
    """Apply stable level colors without relying on QTextBrowser HTML."""

    def __init__(self, document) -> None:
        super().__init__(document)
        self.formats: dict[str, QTextCharFormat] = {}
        for level, color in {
            "HATA": "#ff9f9f",
            "UYARI": "#ffd58a",
            "BAŞARILI": "#9ee7b7",
            "BİLGİ": "#d9e7ff",
        }.items():
            text_format = QTextCharFormat()
            text_format.setForeground(QColor(color))
            text_format.setFontWeight(QFont.Weight.DemiBold)
            self.formats[level] = text_format

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        for level, text_format in self.formats.items():
            if f"[{level}]" in text:
                self.setFormat(0, len(text), text_format)
                return


class CopyableTableWidget(QTableWidget):
    """Read-only operational tables that can still be copied with Ctrl+C."""

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selected_cells()
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_selected_cells(self) -> None:
        indexes = self.selectedIndexes()
        if not indexes:
            return
        min_row = min(index.row() for index in indexes)
        max_row = max(index.row() for index in indexes)
        min_column = min(index.column() for index in indexes)
        max_column = max(index.column() for index in indexes)
        selected = {(index.row(), index.column()) for index in indexes}
        rows: list[str] = []
        for row in range(min_row, max_row + 1):
            values: list[str] = []
            for column in range(min_column, max_column + 1):
                item = self.item(row, column)
                values.append(item.text() if item is not None and (row, column) in selected else "")
            rows.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(rows))


class CollapsibleSection(QFrame):
    """Acik / kapali calisabilen secenek grubu karti."""
    def __init__(self, title: str, subtitle: str, expanded: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("optionGroup")
        self.toggle_button = QPushButton()
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setObjectName("sectionToggle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("optionSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.body = QWidget()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.toggle_button.clicked.connect(self._apply_state)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.subtitle_label)
        layout.addWidget(self.body)

        self._title = title
        self._apply_state(expanded)

    def set_body_layout(self, content_layout: QVBoxLayout) -> None:
        self.body.setLayout(content_layout)

    def _apply_state(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.subtitle_label.setVisible(expanded)
        prefix = "▾" if expanded else "▸"
        self.toggle_button.setText(f"{prefix}  {self._title}")


class TaskWorker(QObject):
    """Agir servis islerini UI thread'inden ayri calistiran arka plan iscisi."""
    finished = Signal(object)
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, task: Callable[[Callable[[str], None]], object]) -> None:
        super().__init__()
        self.task = task

    @Slot()
    def run(self) -> None:
        try:
            result = self.task(self.log.emit)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


class SettingsPasswordDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ayarlar Kilidi")
        self.setModal(True)
        self.resize(420, 220)
        self.setStyleSheet(
            """
            QDialog {
                background: #f4ecde;
            }
            QLabel {
                color: #2e241b;
                font-family: 'Segoe UI';
            }
            #lockCard {
                background: #fffaf1;
                border: 1px solid #dcc79c;
                border-radius: 22px;
            }
            #lockTitle {
                font-size: 24px;
                font-weight: 800;
            }
            #lockText {
                font-size: 14px;
                color: #715d48;
            }
            QLineEdit {
                min-height: 42px;
                background: #fffefb;
                border: 1px solid #d4c19a;
                border-radius: 15px;
                padding: 0 14px;
                color: #1f1b18;
                font-size: 15px;
            }
            QLineEdit:focus {
                border: 1px solid #b58e3f;
                background: #ffffff;
            }
            QPushButton {
                background: #2b2420;
                color: #f7edd7;
                border: 0;
                border-radius: 14px;
                padding: 11px 18px;
                font-weight: 800;
            }
            QPushButton:hover {
                background: #3a312c;
            }
            QPushButton#primaryButton {
                background: #d0af68;
                color: #1f1b17;
            }
            QPushButton#primaryButton:hover {
                background: #ddb86b;
            }
            """
        )

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Ayar sifresini gir")
        self.password_input.returnPressed.connect(self.accept)

        card = QFrame()
        card.setObjectName("lockCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(12)

        title = QLabel("Ayarlar Korunuyor")
        title.setObjectName("lockTitle")
        text = QLabel("Bu bolum yalnizca yetkili kullanim icin acilir. Devam etmek icin şifre gir.")
        text.setObjectName("lockText")
        text.setWordWrap(True)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton("Vazgec")
        ok_button = QPushButton("Giris Yap")
        ok_button.setObjectName("primaryButton")
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(ok_button)

        card_layout.addWidget(title)
        card_layout.addWidget(text)
        card_layout.addWidget(self.password_input)
        card_layout.addLayout(button_row)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.addWidget(card)

    def is_valid_password(self) -> bool:
        entered = self.password_input.text()
        digest = hashlib.sha256(entered.encode("utf-8")).hexdigest()
        return digest == SETTINGS_PASSWORD_HASH


class SettingsDialog(QDialog):
    """Ayarlar ekranini yoneten pencere.

    Stajyer Notu:
    - Teknik olarak buyuk bir "config editor" gibi davranir.
    - Yeni alan eklerken hem burada widget olusturmak hem de `accept()` akisinda
      config nesnesine geri yazmak gerekir.
    """
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.saved_config: AppConfig | None = None

        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowSystemMenuHint)
        self.setWindowTitle("Ayarlar")
        self.resize(960, 580)
        self.setMinimumSize(680, 420)
        self.setStyleSheet(self._build_stylesheet())

        self.title_input = QLineEdit(config.branding.title)
        self.subtitle_input = QLineEdit(config.branding.subtitle)
        self.logo_path_input = QLineEdit(self._path_to_text(config.branding.logo_path))
        self.local_admin_name_input = QLineEdit(config.tools.local_admin_username)
        self.local_admin_password_input = QLineEdit(config.tools.local_admin_password)
        self.local_admin_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.legacy_cleanup_user_input = QLineEdit(config.legacy_cleanup_user)
        self.anydesk_dir_input = QLineEdit(config.tools.anydesk_install_dir)
        self.anydesk_payload_input = QLineEdit(config.tools.anydesk_installer_path)
        self.eset_path_input = QLineEdit(config.tools.eset_installer_path)
        self.hackbgrt_path_input = QLineEdit(config.tools.hackbgrt_setup_path)
        self.domain_name_input = QLineEdit(config.domain.name)
        self.domain_user_input = QLineEdit(config.domain.username)
        self.domain_password_input = QLineEdit(config.domain.password)
        self.domain_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.general_wifi_ssid_input = QLineEdit(config.wifi_profiles.get("general").ssid if config.wifi_profiles.get("general") else "")
        self.general_wifi_password_input = QLineEdit(config.wifi_profiles.get("general").password if config.wifi_profiles.get("general") else "")
        self.general_wifi_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.domain_wifi_ssid_input = QLineEdit(config.wifi_profiles.get("domain_join").ssid if config.wifi_profiles.get("domain_join") else "")
        self.domain_wifi_password_input = QLineEdit(config.wifi_profiles.get("domain_join").password if config.wifi_profiles.get("domain_join") else "")
        self.domain_wifi_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.backup_path_input = QLineEdit(config.backup.network_path)
        self.backup_user_input = QLineEdit(config.backup.network_user)
        self.backup_password_input = QLineEdit(config.backup.network_password)
        self.backup_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.credential_domain_input = QLineEdit(config.network_resources.credential_domain)
        self.required_wifi_input = QLineEdit(config.network_resources.required_wifi_ssid)
        self.printer_host_input = QLineEdit(config.network_resources.printer_host)
        self.printer_share_input = QLineEdit(config.network_resources.printer_share)
        self.file_server_host_input = QLineEdit(config.network_resources.file_server_host)
        self.file_server_share_input = QLineEdit(config.network_resources.file_server_share)
        self.file_server_shortcut_input = QLineEdit(config.network_resources.file_server_shortcut_name)
        self.reporting_enabled_input = QComboBox()
        self.reporting_enabled_input.addItems(["Evet", "Hayir"])
        self.reporting_enabled_input.setCurrentText("Evet" if config.reporting.enabled else "Hayir")
        self.report_output_dir_input = QLineEdit(self._path_to_text(config.reporting.output_dir))
        self.webhook_url_input = QLineEdit(config.reporting.webhook_url)
        self.webhook_token_input = QLineEdit(config.reporting.webhook_token)
        self.telegram_bot_token_input = QLineEdit(config.reporting.telegram_bot_token)
        self.telegram_chat_id_input = QLineEdit(config.reporting.telegram_chat_id)
        self.signature_source_input = QLineEdit(self._optional_path_to_text(config.desktop_automation.signature_source_dir))
        self.signature_folder_input = QLineEdit(config.desktop_automation.signature_folder_name)
        self.outlook_path_input = QLineEdit(config.desktop_automation.outlook_classic_path)
        self.outlook_email_input = QLineEdit(config.desktop_automation.outlook_email)
        self.outlook_password_input = QLineEdit(config.desktop_automation.outlook_password)
        self.outlook_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.wallpaper_source_input = QLineEdit(self._optional_path_to_text(config.desktop_automation.wallpaper_source_path))
        self.lock_screen_source_input = QLineEdit(self._optional_path_to_text(config.desktop_automation.lock_screen_source_path))
        self.wallpaper_lock_input = QComboBox()
        self.wallpaper_lock_input.addItems(["Evet", "Hayir"])
        self.wallpaper_lock_input.setCurrentText("Evet" if config.desktop_automation.wallpaper_lock_standard_users else "Hayir")
        self.windows_key_input = QLineEdit(config.windows.activation_product_key)
        self.windows_update_uri_input = QLineEdit(config.windows.update_uri)

        self.company_table = QTableWidget(0, 3)
        self.company_table.setHorizontalHeaderLabels(["Şirket", "Prefix", "Varsayılan Şifre"])
        self.company_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.company_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.company_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.company_table.verticalHeader().setVisible(False)
        self.company_table.setAlternatingRowColors(True)
        self.company_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.company_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.company_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )

        self.profile_editor_updating = False
        self.profile_table = QTableWidget(0, 3)
        self.profile_table.setHorizontalHeaderLabels(["Profil", "Kullanıcı Tipi", "Şirket"])
        self.profile_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.profile_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.profile_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.profile_table.verticalHeader().setVisible(False)
        self.profile_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.profile_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.profile_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )

        self.profile_name_input = QLineEdit()
        self.profile_user_type_input = QComboBox()
        self.profile_user_type_input.addItems(config.user_types)
        self.profile_company_input = QComboBox()
        self.profile_company_input.addItem("")
        self.profile_company_input.addItems(list(config.companies.keys()))
        self.profile_note_input = QPlainTextEdit()
        self.profile_note_input.setPlaceholderText("Profilin ne amaçla kullanılacağını kısa not olarak yazabilirsin.")
        self.profile_note_input.setFixedHeight(90)
        self.profile_option_boxes: dict[str, QCheckBox] = {}
        for _, _, options in OPTION_GROUPS:
            for option_name, label, _, default in options:
                box = QCheckBox(label)
                box.setChecked(default)
                self.profile_option_boxes[option_name] = box

        self._build_ui()
        self.populate_company_table()
        self.populate_profile_table()
        self.company_table.itemChanged.connect(self.refresh_profile_company_choices)

    def _path_to_text(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.config.base_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def _optional_path_to_text(self, path: Path) -> str:
        if not path or str(path) == ".":
            return ""
        return self._path_to_text(path)

    def _resolve_path_input(self, raw_value: str, default_value: str = "") -> Path:
        normalized = raw_value.strip() or default_value.strip()
        if not normalized:
            return Path()
        path = Path(normalized)
        if path.is_absolute():
            return path.resolve()
        return (self.config.base_dir / path).resolve()

    def _image_picker(self, input_widget: QLineEdit, caption: str) -> QWidget:
        """Build a compact, explicit image selection control for settings."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(input_widget, 1)
        button = QPushButton("Seç")
        button.setMinimumWidth(72)
        button.clicked.connect(lambda: self._select_image_into(input_widget, caption))
        layout.addWidget(button)
        return row

    def _select_image_into(self, input_widget: QLineEdit, caption: str) -> None:
        start_dir = str(self.config.base_dir / "assets")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            caption,
            start_dir,
            "Resim Dosyaları (*.jpg *.jpeg *.png *.bmp)",
        )
        if file_path:
            input_widget.setText(file_path)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        header = QFrame()
        header.setObjectName("dialogHero")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        header_layout.setSpacing(4)

        title = QLabel("Ayarlar")
        title.setObjectName("dialogTitle")
        subtitle = QLabel("Şirket listesi, lokaladm bilgileri, ağ ayarları ve kurulum yollarını buradan yönetebilirsin.")
        subtitle.setObjectName("dialogSubtitle")
        subtitle.setWordWrap(True)

        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(False)
        tabs.addTab(self.build_general_tab(), "Genel")
        tabs.addTab(self.build_companies_tab(), "Şirketler")
        tabs.addTab(self.build_profiles_tab(), "Profiller")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Reset)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Kaydet")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("İptal")
        buttons.button(QDialogButtonBox.StandardButton.Reset).setText("Varsayılanlara Sıfırla")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(self.reset_to_defaults)

        root.addWidget(header)
        root.addWidget(tabs, 1)
        root.addWidget(buttons)

    def build_general_tab(self) -> QWidget:
        content = QWidget()
        content.setObjectName("settingsScrollContent")
        grid = QGridLayout(content)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        grid.addWidget(self._card("Marka", [
            ("Başlık", self.title_input),
            ("Alt Başlık", self.subtitle_input),
            ("Logo Yolu", self.logo_path_input),
        ]), 0, 0)

        grid.addWidget(self._card("Lokaladm", [
            ("Kullanıcı Adı", self.local_admin_name_input),
            ("Şifre", self.local_admin_password_input),
            ("Silinecek Eski Kullanıcı", self.legacy_cleanup_user_input),
        ]), 0, 1)
        
        grid.addWidget(self._card("Kurulum Dosyaları", [
            ("AnyDesk Klasörü", self.anydesk_dir_input),
            ("AnyDesk Payload", self.anydesk_payload_input),
            ("ESET Dosya Yolu", self.eset_path_input),
            ("HackBGRT Dosya Yolu", self.hackbgrt_path_input),
        ]), 1, 0)

        grid.addWidget(self._card("Domain", [
            ("Domain", self.domain_name_input),
            ("Kullanıcı", self.domain_user_input),
            ("Şifre", self.domain_password_input),
        ]), 1, 1)

        grid.addWidget(self._card("Wi-Fi", [
            ("Genel SSID", self.general_wifi_ssid_input),
            ("Genel Şifre", self.general_wifi_password_input),
            ("Domain SSID", self.domain_wifi_ssid_input),
            ("Domain Şifre", self.domain_wifi_password_input),
        ]), 2, 0)

        grid.addWidget(self._card("Yedekleme", [
            ("Hedef Paylaşım", self.backup_path_input),
            ("Ağ Kullanıcı", self.backup_user_input),
            ("Ağ Şifre", self.backup_password_input),
        ]), 2, 1)

        grid.addWidget(self._card("Ağ Kaynakları", [
            ("Kimlik Domain", self.credential_domain_input),
            ("Zorunlu Wi-Fi", self.required_wifi_input),
            ("Yazıcı Host", self.printer_host_input),
            ("Yazıcı Share", self.printer_share_input),
            ("File Server Host", self.file_server_host_input),
            ("File Server Share", self.file_server_share_input),
            ("Kısayol Adı", self.file_server_shortcut_input),
        ]), 3, 0)

        grid.addWidget(self._card("Raporlama", [
            ("Rapor Açık", self.reporting_enabled_input),
            ("Rapor Klasörü", self.report_output_dir_input),
            ("Webhook URL", self.webhook_url_input),
            ("Webhook Token", self.webhook_token_input),
            ("Telegram Bot", self.telegram_bot_token_input),
            ("Telegram Chat ID", self.telegram_chat_id_input),
        ]), 3, 1)

        grid.addWidget(self._card("Son Kullanıcı", [
            ("İmza Kaynağı", self.signature_source_input),
            ("İmza Klasörü", self.signature_folder_input),
            ("Outlook Yolu", self.outlook_path_input),
            ("Masaüstü Duvar Kâğıdı", self._image_picker(self.wallpaper_source_input, "Masaüstü Duvar Kâğıdı Seç")),
        ]), 4, 0)

        grid.addWidget(self._card("Windows", [
            ("Ürün Anahtarı", self.windows_key_input),
            ("Update URI", self.windows_update_uri_input),
        ]), 4, 1)

        area = QScrollArea()
        area.setObjectName("settingsScrollArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.viewport().setObjectName("settingsViewport")
        area.setWidget(content)
        return area

    def build_companies_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        top_bar = QFrame()
        top_bar.setObjectName("miniToolbar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 10, 12, 10)
        top_layout.setSpacing(10)

        add_button = QPushButton("Şirket Ekle")
        remove_button = QPushButton("Seçileni Sil")
        add_button.clicked.connect(self.add_company_row)
        remove_button.clicked.connect(self.remove_selected_company)

        top_layout.addWidget(add_button)
        top_layout.addWidget(remove_button)
        top_layout.addStretch(1)

        note = QLabel("Şirket adı, prefix ve varsayılan şifreyi doğrudan tablo üzerinde düzenleyebilirsin.")
        note.setObjectName("settingsNote")
        note.setWordWrap(True)

        layout.addWidget(top_bar)
        layout.addWidget(note)
        layout.addWidget(self.company_table, 1)
        return tab

    def build_profiles_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        top_bar = QFrame()
        top_bar.setObjectName("miniToolbar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 10, 12, 10)
        top_layout.setSpacing(10)

        add_button = QPushButton("Profil Ekle")
        remove_button = QPushButton("Seçileni Sil")
        add_button.clicked.connect(self.add_profile_row)
        remove_button.clicked.connect(self.remove_selected_profile)

        top_layout.addWidget(add_button)
        top_layout.addWidget(remove_button)
        top_layout.addStretch(1)

        note = QLabel("Hazır kurulum şablonlarını buradan yönetebilirsin. Satır seçince not ve açık adımlar aşağıda düzenlenir.")
        note.setObjectName("settingsNote")
        note.setWordWrap(True)

        content = QSplitter(Qt.Orientation.Horizontal)
        content.setChildrenCollapsible(False)

        table_shell = QFrame()
        table_layout = QVBoxLayout(table_shell)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(10)
        table_layout.addWidget(self.profile_table)

        editor_shell = QGroupBox("Profil Detayı")
        editor_layout = QVBoxLayout(editor_shell)
        editor_layout.setContentsMargins(4, 4, 4, 4)
        editor_layout.setSpacing(0)

        # Sıkışıklığı önlemek için tüm form ve checkboxları içeren bir scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(12)
        scroll_layout.setContentsMargins(10, 10, 10, 10)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.addRow("Profil Adı", self.profile_name_input)
        form.addRow("Kullanıcı Tipi", self.profile_user_type_input)
        form.addRow("Şirket", self.profile_company_input)
        scroll_layout.addLayout(form)

        note_label = QLabel("Profil Notu")
        note_label.setObjectName("fieldLabel")
        scroll_layout.addWidget(note_label)
        scroll_layout.addWidget(self.profile_note_input)

        options_box = QGroupBox("Açık Adımlar")
        options_layout = QGridLayout(options_box)
        options_layout.setHorizontalSpacing(10)
        options_layout.setVerticalSpacing(10)
        row = 0
        col = 0
        for option_name, box in self.profile_option_boxes.items():
            options_layout.addWidget(box, row, col)
            col += 1
            if col == 2:
                col = 0
                row += 1
        scroll_layout.addWidget(options_box)
        scroll_layout.addStretch(1)

        scroll.setWidget(scroll_content)
        editor_layout.addWidget(scroll)

        content.addWidget(table_shell)
        content.addWidget(editor_shell)
        content.setSizes([350, 650])

        self.profile_table.itemSelectionChanged.connect(self.load_selected_profile_into_editor)
        self.profile_name_input.textChanged.connect(self.sync_profile_editor_to_row)
        self.profile_user_type_input.currentTextChanged.connect(self.sync_profile_editor_to_row)
        self.profile_company_input.currentTextChanged.connect(self.sync_profile_editor_to_row)
        self.profile_note_input.textChanged.connect(self.sync_profile_editor_to_row)
        for box in self.profile_option_boxes.values():
            box.toggled.connect(self.sync_profile_editor_to_row)

        layout.addWidget(top_bar)
        layout.addWidget(note)
        layout.addWidget(content, 1)
        return tab

    def _card(self, title: str, rows: list[tuple[str, QWidget]]) -> QWidget:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        for label, widget in rows:
            form.addRow(label, widget)
        return box

    def populate_company_table(self) -> None:
        self.company_table.setRowCount(0)
        for name, company in self.config.companies.items():
            self.add_company_row(name, company.prefix, company.password)
        self.refresh_profile_company_choices()

    def populate_profile_table(self) -> None:
        self.profile_table.setRowCount(0)
        for name, profile in self.config.profiles.items():
            self.add_profile_row(name, profile.user_type, profile.company_name, profile.note, profile.options)
        if self.profile_table.rowCount() > 0:
            self.profile_table.selectRow(0)
            self.load_selected_profile_into_editor()

    def add_company_row(self, name: str = "", prefix: str = "", password: str = "") -> None:
        row = self.company_table.rowCount()
        self.company_table.insertRow(row)
        self.company_table.setItem(row, 0, QTableWidgetItem(name))
        self.company_table.setItem(row, 1, QTableWidgetItem(prefix))
        self.company_table.setItem(row, 2, QTableWidgetItem(password))
        self.company_table.setRowHeight(row, 42)

    def add_profile_row(
        self,
        name: str = "",
        user_type: str = "Lokal",
        company_name: str = "",
        note: str = "",
        options: dict[str, bool] | None = None,
    ) -> None:
        row = self.profile_table.rowCount()
        self.profile_table.insertRow(row)
        name_item = QTableWidgetItem(name or f"Yeni Profil {row + 1}")
        metadata = {
            "note": note,
            "options": options.copy() if options else self._default_profile_options(),
        }
        name_item.setData(Qt.ItemDataRole.UserRole, metadata)
        self.profile_table.setItem(row, 0, name_item)
        self.profile_table.setItem(row, 1, QTableWidgetItem(user_type or "Lokal"))
        self.profile_table.setItem(row, 2, QTableWidgetItem(company_name))
        self.profile_table.setRowHeight(row, 42)
        self.profile_table.selectRow(row)
        self.load_selected_profile_into_editor()

    def remove_selected_company(self) -> None:
        row = self.company_table.currentRow()
        if row >= 0:
            self.company_table.removeRow(row)
        self.refresh_profile_company_choices()

    def remove_selected_profile(self) -> None:
        row = self.profile_table.currentRow()
        if row >= 0:
            self.profile_table.removeRow(row)
        if self.profile_table.rowCount() > 0:
            self.profile_table.selectRow(min(row, self.profile_table.rowCount() - 1))
            self.load_selected_profile_into_editor()

    def _item_text(self, row: int, column: int) -> str:
        item = self.company_table.item(row, column)
        return item.text().strip() if item else ""

    def _profile_item_text(self, row: int, column: int) -> str:
        item = self.profile_table.item(row, column)
        return item.text().strip() if item else ""

    def _default_profile_options(self) -> dict[str, bool]:
        return {name: default for _, _, options in OPTION_GROUPS for name, _, _, default in options}

    def refresh_profile_company_choices(self) -> None:
        current = self.profile_company_input.currentText().strip()
        company_names = []
        for row in range(self.company_table.rowCount()):
            name = self._item_text(row, 0)
            if name:
                company_names.append(name)
        self.profile_company_input.blockSignals(True)
        self.profile_company_input.clear()
        self.profile_company_input.addItem("")
        self.profile_company_input.addItems(company_names)
        if current and self.profile_company_input.findText(current) >= 0:
            self.profile_company_input.setCurrentText(current)
        else:
            self.profile_company_input.setCurrentIndex(0)
        self.profile_company_input.blockSignals(False)

    def load_selected_profile_into_editor(self) -> None:
        row = self.profile_table.currentRow()
        if row < 0:
            return
        item = self.profile_table.item(row, 0)
        metadata = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(metadata, dict):
            metadata = {"note": "", "options": self._default_profile_options()}
        options = metadata.get("options", self._default_profile_options())

        self.profile_editor_updating = True
        self.profile_name_input.setText(self._profile_item_text(row, 0))
        self.profile_user_type_input.setCurrentText(self._profile_item_text(row, 1) or "Lokal")
        company_value = self._profile_item_text(row, 2)
        if company_value and self.profile_company_input.findText(company_value) >= 0:
            self.profile_company_input.setCurrentText(company_value)
        else:
            self.profile_company_input.setCurrentIndex(0)
        self.profile_note_input.setPlainText(str(metadata.get("note", "")))
        for option_name, box in self.profile_option_boxes.items():
            box.setChecked(bool(options.get(option_name, False)))
        self.profile_editor_updating = False

    def sync_profile_editor_to_row(self) -> None:
        if self.profile_editor_updating:
            return
        row = self.profile_table.currentRow()
        if row < 0:
            return
        name = self.profile_name_input.text().strip() or f"Yeni Profil {row + 1}"
        user_type = self.profile_user_type_input.currentText().strip() or "Lokal"
        company_name = self.profile_company_input.currentText().strip()
        metadata = {
            "note": self.profile_note_input.toPlainText().strip(),
            "options": {option_name: box.isChecked() for option_name, box in self.profile_option_boxes.items()},
        }

        if self.profile_table.item(row, 0) is None:
            self.profile_table.setItem(row, 0, QTableWidgetItem())
        if self.profile_table.item(row, 1) is None:
            self.profile_table.setItem(row, 1, QTableWidgetItem())
        if self.profile_table.item(row, 2) is None:
            self.profile_table.setItem(row, 2, QTableWidgetItem())

        self.profile_table.item(row, 0).setText(name)
        self.profile_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, metadata)
        self.profile_table.item(row, 1).setText(user_type)
        self.profile_table.item(row, 2).setText(company_name)

    def build_config(self) -> AppConfig:
        new_config = load_app_config(self.config.base_dir)

        companies: dict[str, CompanyProfile] = {}
        for row in range(self.company_table.rowCount()):
            name = self._item_text(row, 0)
            prefix = self._item_text(row, 1)
            password = self._item_text(row, 2)
            if not name:
                continue
            if name in companies:
                raise RuntimeError(f"Aynı şirket iki kez girildi: {name}")
            companies[name] = CompanyProfile(prefix=prefix, password=password)

        if not companies:
            raise RuntimeError("En az bir şirket kalmalı.")

        profiles: dict[str, dict[str, object]] = {}
        for row in range(self.profile_table.rowCount()):
            name = self._profile_item_text(row, 0)
            user_type = self._profile_item_text(row, 1) or "Lokal"
            company_name = self._profile_item_text(row, 2)
            if not name:
                continue
            if name in profiles:
                raise RuntimeError(f"Aynı profil iki kez girildi: {name}")
            item = self.profile_table.item(row, 0)
            metadata = item.data(Qt.ItemDataRole.UserRole) if item else None
            if not isinstance(metadata, dict):
                metadata = {"note": "", "options": self._default_profile_options()}
            profiles[name] = {
                "user_type": user_type,
                "company_name": company_name,
                "note": str(metadata.get("note", "")),
                "options": {
                    option_name: bool(option_value)
                    for option_name, option_value in dict(metadata.get("options", {})).items()
                },
            }
        if not profiles:
            raise RuntimeError("En az bir profil kalmalı.")

        new_config.branding.title = self.title_input.text().strip() or "AÇIK Kurulum"
        new_config.branding.subtitle = self.subtitle_input.text().strip()
        new_config.branding.logo_path = self._resolve_path_input(
            self.logo_path_input.text(),
            self._path_to_text(self.config.branding.logo_path),
        )
        new_config.companies = companies
        new_config.profiles = {
            profile_name: WorkflowProfile(
                user_type=str(values["user_type"]),
                company_name=str(values["company_name"]),
                note=str(values["note"]),
                options=dict(values["options"]),
            )
            for profile_name, values in profiles.items()
        }
        new_config.tools.local_admin_username = self.local_admin_name_input.text().strip() or "lokaladm"
        new_config.tools.local_admin_password = self.local_admin_password_input.text().strip()
        new_config.legacy_cleanup_user = self.legacy_cleanup_user_input.text().strip() or "x"
        new_config.tools.anydesk_install_dir = self.anydesk_dir_input.text().strip()
        new_config.tools.anydesk_installer_path = self.anydesk_payload_input.text().strip()
        new_config.tools.eset_installer_path = self.eset_path_input.text().strip()
        new_config.tools.hackbgrt_setup_path = self.hackbgrt_path_input.text().strip()
        new_config.domain.name = self.domain_name_input.text().strip()
        new_config.domain.username = self.domain_user_input.text().strip()
        new_config.domain.password = self.domain_password_input.text().strip()
        new_config.wifi_profiles["general"].ssid = self.general_wifi_ssid_input.text().strip()
        new_config.wifi_profiles["general"].password = self.general_wifi_password_input.text().strip()
        new_config.wifi_profiles["domain_join"].ssid = self.domain_wifi_ssid_input.text().strip()
        new_config.wifi_profiles["domain_join"].password = self.domain_wifi_password_input.text().strip()
        new_config.backup.network_path = self.backup_path_input.text().strip()
        new_config.backup.network_user = self.backup_user_input.text().strip()
        new_config.backup.network_password = self.backup_password_input.text().strip()
        new_config.network_resources.credential_domain = self.credential_domain_input.text().strip() or "ACIK"
        new_config.network_resources.required_wifi_ssid = self.required_wifi_input.text().strip()
        new_config.network_resources.printer_host = self.printer_host_input.text().strip()
        new_config.network_resources.printer_share = self.printer_share_input.text().strip()
        new_config.network_resources.file_server_host = self.file_server_host_input.text().strip()
        new_config.network_resources.file_server_share = self.file_server_share_input.text().strip()
        new_config.network_resources.file_server_shortcut_name = self.file_server_shortcut_input.text().strip() or "FileServer"
        new_config.reporting.enabled = self.reporting_enabled_input.currentText() == "Evet"
        new_config.reporting.output_dir = self._resolve_path_input(
            self.report_output_dir_input.text(),
            "runtime/reports",
        )
        new_config.reporting.webhook_url = self.webhook_url_input.text().strip()
        new_config.reporting.webhook_token = self.webhook_token_input.text().strip()
        new_config.reporting.telegram_bot_token = self.telegram_bot_token_input.text().strip()
        new_config.reporting.telegram_chat_id = self.telegram_chat_id_input.text().strip()
        new_config.desktop_automation.signature_source_dir = self._resolve_path_input(
            self.signature_source_input.text(),
        )
        new_config.desktop_automation.signature_folder_name = self.signature_folder_input.text().strip() or "Imza"
        new_config.desktop_automation.outlook_classic_path = self.outlook_path_input.text().strip()
        # Outlook oturum acma bilgileri uygulamada tutulmaz. Microsoft 365
        # kimlik dogrulamasi kullanici/MFA akisi ile tamamlanir.
        new_config.desktop_automation.outlook_email = ""
        new_config.desktop_automation.outlook_password = ""
        new_config.desktop_automation.wallpaper_source_path = self._resolve_path_input(
            self.wallpaper_source_input.text(),
            "assets/wallpaper.jpg",
        )
        new_config.desktop_automation.wallpaper_target_path = new_config.desktop_automation.wallpaper_source_path
        new_config.desktop_automation.lock_screen_source_path = self._resolve_path_input(
            self.lock_screen_source_input.text(),
            "assets/uyku modu.jpg",
        )
        new_config.desktop_automation.lock_screen_target_path = new_config.desktop_automation.lock_screen_source_path
        new_config.desktop_automation.wallpaper_lock_standard_users = self.wallpaper_lock_input.currentText() == "Evet"
        new_config.windows.activation_product_key = self.windows_key_input.text().strip()
        new_config.windows.update_uri = self.windows_update_uri_input.text().strip() or "ms-settings:windowsupdate"
        return new_config

    def accept(self) -> None:
        try:
            built = self.build_config()
            validation_errors = validate_app_config(built)
            if validation_errors:
                raise ValueError(
                    "Ayarlar kaydedilmeden once su alanlar duzeltilmeli:\n- "
                    + "\n- ".join(validation_errors)
                )
            save_app_config(built)
            self.saved_config = load_app_config(self.config.base_dir)
        except Exception as exc:  # noqa: BLE001
            if hasattr(self.parent(), "show_message"):
                self.parent().show_message("Ayarlar Kaydedilemedi", str(exc), "error")
            else:
                QMessageBox.critical(self, "Ayarlar Kaydedilemedi", str(exc))
            return
        super().accept()

    def reset_to_defaults(self) -> None:
        if hasattr(self.parent(), "ask_confirmation"):
            confirmed = self.parent().ask_confirmation(
                "Varsayılanlara Dön", 
                "Mevcut tüm ayarlar silinip programın orijinal varsayılan ayarlarına dönülecektir.\nEmin misiniz?"
            )
        else:
            reply = QMessageBox.question(
                self, 
                "Varsayılanlara Dön", 
                "Mevcut tüm ayarlar silinip programın orijinal varsayılan ayarlarına dönülecektir.\nEmin misiniz?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            confirmed = (reply == QMessageBox.StandardButton.Yes)

        if confirmed:
            local_path = self.config.base_dir / "app_config.local.json"
            if local_path.exists():
                try:
                    local_path.unlink()
                except Exception as e:
                    if hasattr(self.parent(), "show_message"):
                        self.parent().show_message("Hata", f"Ayar dosyası silinirken hata oluştu: {e}", "error")
                    else:
                        QMessageBox.warning(self, "Hata", f"Ayar dosyası silinirken hata oluştu: {e}")
                    return
            
            if hasattr(self.parent(), "show_message"):
                self.parent().show_message("Başarılı", "Ayarlar varsayılana sıfırlandı. Uygulama kapatılacak. Lütfen yeniden başlatın.", "info")
            else:
                QMessageBox.information(self, "Başarılı", "Ayarlar varsayılana sıfırlandı. Uygulama kapatılacak. Lütfen yeniden başlatın.")
            import sys
            sys.exit(0)

    def _build_stylesheet(self) -> str:
        return """
        QDialog {
            background: #f7f3eb;
        }
        QWidget {
            color: #211f1d;
            font-family: 'Segoe UI';
            font-size: 14px;
            background: transparent;
        }
        QLabel {
            background: transparent;
        }
        #dialogHero {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #fffaf0, stop:1 #f4e7ce);
            border: 1px solid #decca3;
            border-radius: 20px;
        }
        #dialogTitle {
            font-size: 26px;
            font-weight: 800;
            color: #2b2623;
        }
        #dialogSubtitle {
            color: #6f6557;
            font-size: 14px;
        }
        QGroupBox {
            background: #fffdf9;
            border: 1px solid #e1d1b0;
            border-radius: 18px;
            margin-top: 14px;
            padding: 18px;
            font-weight: 700;
            color: #54402c;
        }
        QGroupBox::title {
            left: 12px;
            padding: 0 6px;
        }
        QLineEdit, QTableWidget, QComboBox, QPlainTextEdit {
            background: #fffefb;
            border: 1px solid #cfbb95;
            border-radius: 14px;
            padding: 10px 12px;
            color: #1f1b18;
        }
        QComboBox {
            padding-right: 44px;
            min-height: 42px;
        }
        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 36px;
            border-left: 1px solid #d8c49d;
            background: #f3e4c4;
            border-top-right-radius: 14px;
            border-bottom-right-radius: 14px;
        }
        QComboBox::down-arrow {
            width: 0;
            height: 0;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 6px solid #4c3928;
        }
        QComboBox QAbstractItemView {
            background: #fffdf9;
            color: #1f1b18;
            selection-background-color: #efdfba;
            selection-color: #1f1b18;
            border: 1px solid #cfbb95;
            outline: 0;
        }
        QCheckBox {
            spacing: 8px;
            color: #443527;
            font-weight: 600;
        }
        QCheckBox:disabled {
            color: #9e9b97;
        }
        QCheckBox::indicator {
            width: 18px;
            height: 18px;
            border-radius: 6px;
            border: 1px solid #c7b08a;
            background: #fffefb;
        }
        QCheckBox::indicator:checked {
            background: #d0af68;
            border-color: #ba954d;
        }
        QCheckBox::indicator:disabled {
            background: #e8e6e1;
            border: 1px solid #dcd9d4;
        }
        QLineEdit:focus, QComboBox:focus {
            border: 1px solid #b58e3f;
        }
        QScrollArea, #settingsViewport, #settingsScrollContent {
            background: #f7f3eb;
        }
        QTableWidget {
            padding: 0;
            alternate-background-color: #fbf6ec;
        }
        QHeaderView::section {
            background: #efe2c5;
            color: #4a3928;
            border: none;
            border-right: 1px solid #dfcfaf;
            padding: 10px;
            font-weight: 700;
        }
        #miniToolbar {
            background: #fffaf0;
            border: 1px solid #e3d3b1;
            border-radius: 16px;
        }
        #settingsNote {
            color: #665b4d;
        }
        QPushButton {
            background: #262324;
            color: #f5e9c8;
            border: 0;
            border-radius: 14px;
            padding: 10px 18px;
            font-weight: 700;
        }
        QPushButton:hover {
            background: #3a312c;
        }
        QDialogButtonBox QPushButton:first-child {
            background: #d0af68;
            color: #1f1b17;
        }
        QDialogButtonBox QPushButton:first-child:hover {
            background: #ddb86b;
        }
        QTabWidget::pane {
            border: none;
            background: transparent;
        }
        QTabBar::tab {
            background: #ebdfc8;
            color: #4c3a27;
            border: 1px solid #decdaa;
            padding: 10px 18px;
            border-top-left-radius: 12px;
            border-top-right-radius: 12px;
            margin-right: 6px;
        }
        QTabBar::tab:selected {
            background: #fffdf9;
            color: #2c2621;
        }
        """


class CountdownDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        timeout_seconds: int = 60,
        allow_cancel: bool = True,
    ):
        super().__init__(parent)
        self.remaining_seconds = timeout_seconds
        self.allow_cancel = allow_cancel
        self.cancelled = False
        
        self.setWindowTitle("Sistem Yeniden Başlatılıyor")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)
        self.setFixedSize(420, 200)
        
        # Design layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        self.label = QLabel(
            f"Kurulum adımları tamamlandı ve rapor kaydedildi!\n\n"
            f"Bilgisayar {self.remaining_seconds} saniye içinde otomatik olarak yeniden başlatılacaktır.\n"
            f"Lütfen açık belgelerinizi kaydedin.",
            self
        )
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        
        # Buttons layout
        btn_layout = QHBoxLayout()
        self.btn_restart = QPushButton("Şimdi Yeniden Başlat", self)
        self.btn_cancel = QPushButton("İptal Et", self)
        
        # Styling buttons
        self.btn_restart.setStyleSheet("""
            QPushButton {
                background-color: #d0af68;
                color: #1b1e23;
                font-weight: bold;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #e5c380;
            }
        """)
        self.btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #2e3440;
                color: #d8dee9;
                border: 1px solid #4c566a;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #3b4252;
            }
        """)
        
        self.btn_restart.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.cancel_reboot)

        btn_layout.addWidget(self.btn_restart)
        if self.allow_cancel:
            btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)
        
        # Timer setup
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_countdown)
        self.timer.start()
        self.refresh_message()
        
        self.setStyleSheet("""
            QDialog {
                background-color: #1e222b;
                color: #d8dee9;
            }
            QLabel {
                color: #d8dee9;
                font-size: 13px;
            }
        """)

    def refresh_message(self) -> None:
        if self.allow_cancel:
            detail = "Lutfen acik belgelerinizi kaydedin."
        else:
            detail = (
                "X kullanicisi silme islemi secildi. Islem iptal edilemez; "
                "hesap ve profil dogrulanarak silindikten sonra yeniden baslatilacak."
            )
        self.label.setText(
            "Kurulum adimlari tamamlandi ve rapor kaydedildi!\n\n"
            f"Bilgisayar {self.remaining_seconds} saniye icinde otomatik olarak yeniden baslatilacak.\n"
            f"{detail}"
        )

    def update_countdown(self):
        # ESET artik ikinci fazda (SYSTEM finalizasyonu) calisir ve bu diyalog
        # sabit 60 saniyelik tek bir zaman asimiyla acilir; bu yuzden burada
        # ESET yukleyicisini bekleyip son yeniden baslatmayi (ve X temizligini)
        # sure sinirsiz duraklatan eski davranis kaldirildi.
        self.remaining_seconds -= 1
        if self.remaining_seconds <= 0:
            self.timer.stop()
            self.accept()
        else:
            if not self.allow_cancel:
                self.refresh_message()
                return
            self.label.setText(
                f"Kurulum adımları tamamlandı ve rapor kaydedildi!\n\n"
                f"Bilgisayar {self.remaining_seconds} saniye içinde otomatik olarak yeniden başlatılacaktır.\n"
                f"Lütfen açık belgelerinizi kaydedin."
            )

    def cancel_reboot(self):
        if not self.allow_cancel:
            return
        self.timer.stop()
        self.cancelled = True
        self.reject()

    def reject(self) -> None:
        if self.allow_cancel:
            super().reject()


class MainWindow(QMainWindow):
    """Uygulamanin ana onboarding penceresi."""
    def __init__(self, config: AppConfig, service: OnboardingService) -> None:
        super().__init__()
        self.config = config
        self.service = service
        self.current_thread: QThread | None = None
        self.current_worker: TaskWorker | None = None
        self.task_busy = False
        self.current_task_name = ""
        self.current_finish_handler: Callable[[object], None] | None = None
        self.busy_started_at: datetime | None = None
        self.last_onboarding_request: OnboardingRequest | None = None
        self.active_profile_name = ""
        self.settings_failed_attempts = 0
        self.settings_locked_until = 0.0

        self.logo_label = QLabel()
        self.brand_title = QLabel()
        self.brand_subtitle = QLabel()
        self.header_title = QLabel()
        self.header_subtitle = QLabel()
        self.quick_note = QLabel()
        self.profile_note = QLabel()
        self.admin_chip = QLabel("Yönetici Modu")
        self.config_chip = QLabel()
        self.summary_profile_value = QLabel()
        self.summary_company_value = QLabel()
        self.summary_user_type_value = QLabel()
        self.summary_username_value = QLabel()
        self.summary_pc_value = QLabel()
        self.summary_steps_value = QLabel()
        self.full_name_input = QLineEdit()
        self.company_combo = QComboBox()
        self.user_type_combo = QComboBox()
        self.profile_combo = QComboBox()
        self.username_output = QLineEdit()
        self.pc_name_output = QLineEdit()
        self.password_output = QLineEdit()
        self.password_output.setEchoMode(QLineEdit.EchoMode.Password)
        self.company_password_status = QLabel()
        self.company_password_status.setObjectName("companyPasswordStatus")
        self.company_password_status.setWordWrap(True)
        self.domain_signin_hint = QLabel()
        self.domain_signin_hint.setObjectName("companyPasswordStatus")
        self.domain_signin_hint.setWordWrap(True)
        self.domain_signin_hint.hide()
        self.destination_input = QLineEdit()
        self.backup_user_combo = QComboBox()
        self.backup_source_input = QLineEdit()
        # QTextBrowser.setHtml() Qt 6.11'de bazı Intel/uzak ekran
        # sürücülerinde native Qt6Gui çökmesine neden olabiliyor. Canlı log
        # zengin metin gerektirmediği için daha hafif QPlainTextEdit kullanılır.
        self.log_output = QPlainTextEdit()
        self.preflight_button = QPushButton("Sistemi Kontrol Et")
        self.apply_profile_button = QPushButton("Profili Uygula")
        self.generate_button = QPushButton("Bilgileri Üret")
        self.create_button = QPushButton("Kurulumu Başlat")
        self.terminate_button = QPushButton("Süreci Sonlandır")
        self.clear_log_button = QPushButton("Temizle")
        self.domain_leave_button = QPushButton("Eski Domainden Çık")
        self.domain_leave_button.setObjectName("secondaryButton")
        self.backup_button = QPushButton("Yedeklemeyi Başlat")
        self.settings_button = QPushButton("Ayarlar")
        self.compact_settings_button = QPushButton("Ayarlar")
        self.compact_settings_button.setObjectName("secondaryButton")
        self.compact_preflight_status = QLabel("Ön Kontrol: Bekliyor")
        self.compact_preflight_status.setObjectName("statusChipMuted")
        self.compact_step_status = QLabel("Adım: Bekliyor")
        self.compact_step_status.setObjectName("statusChipMuted")
        self.compact_toolbar = QFrame()
        self.compact_toolbar.setObjectName("miniToolbar")
        self.compact_toolbar.hide()
        self.checkboxes: dict[str, QPushButton] = {}
        self.usb_checkboxes: dict[str, QCheckBox] = {}
        self.usb_status_labels: dict[str, QLabel] = {}
        self.usb_install_buttons: dict[str, QPushButton] = {}
        self.usb_connect_buttons: dict[str, QPushButton] = {}
        self.action_title_labels: dict[str, QLabel] = {}
        self.app_check_running = False
        self.app_check_queue: list[str] = []
        self.option_sections: list[CollapsibleSection] = []
        self.log_entries: list[dict[str, str]] = []
        self.current_log_filter = "all"
        self.current_running_step = ""
        self.preflight_status_label = QLabel("Ön kontrol henüz çalıştırılmadı.")
        self.preflight_table = CopyableTableWidget(0, 3)
        self.step_table = CopyableTableWidget(0, 3)
        self.step_hint_label = QLabel("Kurulum başlamadan önce açık adımlara göre akış listesi hazırlanır.")
        self.report_table = CopyableTableWidget(0, 5)
        self.report_detail = QTextBrowser()
        self.report_refresh_button = QPushButton("Raporları Yenile")
        self.report_delete_button = QPushButton("Tüm Raporları Sil")
        self.report_delete_button.setStyleSheet("background-color: #ff6b6b; color: #1f1b17; font-weight: bold;")
        self.report_delete_button.setMinimumWidth(170)
        self.report_delete_button.setMinimumHeight(44)
        self.report_refresh_button.setMinimumWidth(140)
        self.report_refresh_button.setMinimumHeight(44)
        self.report_summary_label = QLabel("Henüz rapor bulunmuyor.")
        self.activity_frame = QFrame()
        self.activity_frame.setObjectName("activityFrame")
        self.activity_title = QLabel("İşlem devam ediyor")
        self.activity_title.setObjectName("activityTitle")
        self.activity_detail = QLabel("Hazırlanıyor...")
        self.activity_detail.setObjectName("activityDetail")
        self.activity_detail.setWordWrap(True)
        self.activity_elapsed = QLabel("00:00")
        self.activity_elapsed.setObjectName("activityElapsed")
        self.activity_progress = QProgressBar()
        self.activity_progress.setObjectName("activityProgress")
        self.activity_progress.setRange(0, 0)
        self.activity_progress.setTextVisible(False)
        self.activity_frame.hide()
        self.activity_timer = QTimer(self)
        self.activity_timer.setInterval(1000)
        self.activity_timer.timeout.connect(self.update_activity_elapsed)
        self.log_filter_buttons: dict[str, QPushButton] = {}
        self.log_filter_grid: QGridLayout | None = None
        self.log_title: QLabel | None = None
        self.log_subtitle: QLabel | None = None
        self.log_filter_shell: QFrame | None = None
        # The log is useful during installation, but it should not consume the
        # form's working space before the operator has started a setup run.
        self.log_expanded_for_installation = False
        self.responsive_mode = ""
        self.root_layout: QHBoxLayout | None = None
        self.sidebar_scroll_area: QScrollArea | None = None
        self.sidebar_panel: QFrame | None = None
        self.main_splitter: QSplitter | None = None
        self.main_tabs: QTabWidget | None = None
        self.onboarding_scroll_area: QScrollArea | None = None
        self.log_panel: QFrame | None = None
        self.summary_grid: QGridLayout | None = None
        self.summary_tiles: list[QWidget] = []
        self.device_summary_grid: QGridLayout | None = None
        self.device_summary_tiles: list[QWidget] = []
        self.profile_actions_grid: QGridLayout | None = None
        self.profile_profile_field: QWidget | None = None
        self.identity_grid: QGridLayout | None = None
        self.identity_fields: list[QWidget] = []
        self.options_group_grid: QGridLayout | None = None
        self.option_group_widgets: list[QWidget] = []
        self.report_splitter: QSplitter | None = None

        self.preflight_button.setObjectName("secondaryButton")
        self.apply_profile_button.setObjectName("secondaryButton")
        self.generate_button.setObjectName("secondaryButton")
        self.create_button.setObjectName("primaryButton")
        self.terminate_button.setObjectName("secondaryButton")
        self.clear_log_button.setObjectName("dangerButton")
        self.clear_log_button.setFixedSize(78, 28)
        self.backup_button.setObjectName("primaryButton")
        self.settings_button.setObjectName("sidebarButton")

        for combo in (self.user_type_combo, self.company_combo, self.profile_combo):
            combo.setEditable(True)
            combo.lineEdit().setReadOnly(True)
            combo.lineEdit().setCursor(Qt.CursorShape.ArrowCursor)

        self.user_type_combo.setEnabled(False)
        self.profile_combo.wheelEvent = lambda event: event.ignore()

        self.preflight_table.setHorizontalHeaderLabels(["Kontrol", "Durum", "Detay"])
        self.preflight_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.preflight_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.preflight_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preflight_table.verticalHeader().setVisible(False)
        self.preflight_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preflight_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.preflight_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.preflight_table.setAlternatingRowColors(True)
        self.preflight_table.setObjectName("sidebarTable")
        self.preflight_table.horizontalHeader().setMinimumSectionSize(74)

        self.step_table.setHorizontalHeaderLabels(["Adım", "Durum", "Detay"])
        self.step_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.step_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.step_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.step_table.verticalHeader().setVisible(False)
        self.step_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.step_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.step_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.step_table.setAlternatingRowColors(True)
        self.step_table.setObjectName("sidebarTable")
        self.step_table.horizontalHeader().setMinimumSectionSize(74)

        self.report_table.setHorizontalHeaderLabels(["Başlangıç", "Durum", "Şirket", "Kullanıcı", "Cihaz"])
        self.report_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.report_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.report_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.report_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.report_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.report_table.verticalHeader().setVisible(False)
        self.report_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.report_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.report_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.report_table.setAlternatingRowColors(True)

        self.report_detail.setReadOnly(True)
        self.report_detail.setPlaceholderText("Listeden bir rapor seçtiğinde detaylar burada görünür.")
        self.report_detail.setObjectName("reportDetail")
        self.log_output.setObjectName("logOutput")
        self.log_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_output.setMaximumBlockCount(5000)
        self.log_output.setUndoRedoEnabled(False)
        self.log_highlighter = LiveLogHighlighter(self.log_output.document())

        self.setWindowTitle(config.branding.title + " - Son Gökçe Ver")
        self.resize(1460, 880)
        self.setMinimumSize(1040, 680)
        self.setStyleSheet(self.build_stylesheet())

        self._build_ui()
        self._connect_signals()
        self.apply_config_to_widgets()
        QTimer.singleShot(0, self.repair_visible_texts)

        # İlk sekmedeki HackBGRT / ESET durumlarını USB sekmesiyle eşitle
        for name in ("hackbgrt", "eset"):
            toggle = self.checkboxes.get(name)
            usb_cb = self.usb_checkboxes.get(name)
            if toggle and usb_cb:
                usb_cb.setChecked(toggle.isChecked())

        self.refresh_responsive_layout()
        QTimer.singleShot(200, self.run_startup_tasks)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        root = QHBoxLayout(central)
        self.root_layout = root
        root.setContentsMargins(22, 22, 22, 22)
        root.setSpacing(20)

        root.addWidget(self.build_sidebar())

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self.build_main_area())
        self.main_splitter.addWidget(self.build_log_panel())
        self.main_splitter.setStretchFactor(0, 10)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setSizes([1200, 280])

        root.addWidget(self.main_splitter, 1)
        self.setCentralWidget(central)

    def build_sidebar(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidebar")
        panel.setMinimumWidth(200)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)

        brand_card = QFrame()
        brand_card.setObjectName("brandCard")
        brand_layout = QVBoxLayout(brand_card)
        brand_layout.setContentsMargins(12, 12, 12, 12)
        brand_layout.setSpacing(8)

        logo_shell = QFrame()
        logo_shell.setObjectName("logoShell")
        logo_shell_layout = QVBoxLayout(logo_shell)
        logo_shell_layout.setContentsMargins(6, 6, 6, 6)
        logo_shell_layout.setSpacing(0)

        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setFixedHeight(90)
        logo_shell_layout.addWidget(self.logo_label)
        self.brand_title.setObjectName("brandTitle")
        self.brand_title.setWordWrap(True)
        self.brand_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_subtitle.setObjectName("brandSubtitle")
        self.brand_subtitle.setWordWrap(True)
        self.brand_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        brand_layout.addWidget(logo_shell)
        brand_layout.addWidget(self.brand_title)
        brand_layout.addWidget(self.brand_subtitle)

        side_card = QFrame()
        side_card.setObjectName("sideCard")
        side_layout = QVBoxLayout(side_card)
        side_layout.setContentsMargins(16, 16, 16, 16)
        side_layout.setSpacing(14)

        status_title = QLabel("Kurulum Akışı")
        status_title.setObjectName("miniTitle")
        status_text = QLabel("Kurulumu başlatmadan önce kısa akış burada net biçimde görünür.")
        status_text.setObjectName("sideText")
        status_text.setWordWrap(True)

        steps_shell = QFrame()
        steps_shell.setObjectName("sidebarSteps")
        steps_layout = QVBoxLayout(steps_shell)
        steps_layout.setContentsMargins(0, 0, 0, 0)
        steps_layout.setSpacing(8)
        for index, text in enumerate(
            [
                "Profili seç",
                "Kimliği üret",
                "Ön kontrolü çalıştır",
                "Kurulumu başlat",
            ],
            start=1,
        ):
            steps_layout.addWidget(self.build_sidebar_step(index, text))

        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        self.admin_chip.setObjectName("statusChip")
        self.config_chip.setObjectName("statusChipMuted")
        chip_row.addWidget(self.admin_chip)
        chip_row.addWidget(self.config_chip)
        chip_row.addStretch(1)

        divider = QFrame()
        divider.setObjectName("sidebarDivider")
        divider.setFrameShape(QFrame.Shape.HLine)

        side_layout.addWidget(status_title)
        side_layout.addWidget(status_text)
        side_layout.addWidget(steps_shell)
        side_layout.addLayout(chip_row)
        side_layout.addWidget(divider)
        side_layout.addWidget(self.settings_button)

        layout.addWidget(brand_card)
        layout.addWidget(side_card)
        layout.addWidget(self.build_preflight_card(compact=True))
        layout.addWidget(self.build_step_tracker_card(compact=True), 1)
        self.sidebar_panel = panel

        self.sidebar_scroll_area = QScrollArea()
        self.sidebar_scroll_area.setWidgetResizable(True)
        self.sidebar_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll_area.setWidget(panel)
        self.sidebar_scroll_area.setFixedWidth(380)
        return self.sidebar_scroll_area

    def build_sidebar_step(self, number: int, text: str) -> QWidget:
        row = QFrame()
        row.setObjectName("sidebarStepRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        number_chip = QLabel(f"{number:02d}")
        number_chip.setObjectName("sidebarStepNumber")
        number_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number_chip.setFixedSize(34, 34)

        text_label = QLabel(text)
        text_label.setObjectName("sidebarStepText")
        text_label.setWordWrap(True)

        layout.addWidget(number_chip)
        layout.addWidget(text_label, 1)
        return row

    def build_device_info_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Cihaz Envanteri")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Donanım, güvenlik ve işletim sistemi bilgileri tek görünümde özetlenir.")
        subtitle.setObjectName("sectionSubtitle")
        subtitle.setWordWrap(True)
        self.log_subtitle = subtitle
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        self.device_info_refresh_button = QPushButton("Envanteri Yenile")
        self.device_info_refresh_button.clicked.connect(self.populate_device_info)
        title_row.addLayout(title_box, 1)
        title_row.addWidget(self.device_info_refresh_button, 0, Qt.AlignmentFlag.AlignTop)

        summary_shell = QFrame()
        summary_shell.setObjectName("deviceSummaryShell")
        summary_layout = QGridLayout(summary_shell)
        self.device_summary_grid = summary_layout
        summary_layout.setContentsMargins(12, 12, 12, 12)
        summary_layout.setHorizontalSpacing(12)
        summary_layout.setVerticalSpacing(12)
        self.device_summary_values: dict[str, QLabel] = {}
        for index, (key, label) in enumerate(
            [
                ("device", "Cihaz"),
                ("serial", "Seri No"),
                ("processor", "İşlemci"),
                ("memory", "Bellek"),
                ("system_disk", "Sistem Diski"),
                ("operating_system", "İşletim Sistemi"),
                ("firmware", "BIOS / Güvenlik"),
                ("graphics", "Grafik"),
            ]
        ):
            value_label = QLabel("Okunuyor…")
            value_label.setObjectName("summaryValue")
            value_label.setWordWrap(True)
            self.device_summary_values[key] = value_label
            copy_button: QToolButton | None = None
            if key == "serial":
                copy_button = QToolButton()
                copy_button.setObjectName("copyIconButton")
                copy_button.setText("⧉")
                copy_button.setToolTip("Seri numarasını panoya kopyala")
                copy_button.setAccessibleName("Seri numarasını kopyala")
                copy_button.clicked.connect(self.copy_device_serial_number)
            tile = self.build_summary_tile(label, value_label, copy_button)
            self.device_summary_tiles.append(tile)
            summary_layout.addWidget(tile, index // 4, index % 4)
            summary_layout.setColumnStretch(index % 4, 1)

        hw_shell = QFrame()
        hw_shell.setObjectName("tableShell")
        hw_layout = QVBoxLayout(hw_shell)
        hw_layout.setContentsMargins(1, 1, 1, 1)
        self.hw_table = CopyableTableWidget()
        self.hw_table.setColumnCount(2)
        self.hw_table.setHorizontalHeaderLabels(["Özellik", "Değer"])
        self.hw_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.hw_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.hw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hw_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.hw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.hw_table.verticalHeader().setVisible(False)
        self.hw_table.setAlternatingRowColors(True)
        self.hw_table.setShowGrid(False)
        self.hw_table.setToolTip("Hücreleri seçip Ctrl+C ile kopyalayabilirsiniz.")
        self.hw_table.setMinimumHeight(300)
        self.hw_table.setMaximumHeight(480)
        hw_layout.addWidget(self.hw_table)

        disk_shell = QFrame()
        disk_shell.setObjectName("tableShell")
        disk_layout = QVBoxLayout(disk_shell)
        disk_layout.setContentsMargins(1, 1, 1, 1)
        self.disk_table = CopyableTableWidget()
        self.disk_table.setColumnCount(5)
        self.disk_table.setHorizontalHeaderLabels(["Aygıt", "Model", "Arayüz", "Kapasite", "Seri No"])
        self.disk_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.disk_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.disk_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.disk_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.disk_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.disk_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.disk_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.disk_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.disk_table.verticalHeader().setVisible(False)
        self.disk_table.setAlternatingRowColors(True)
        self.disk_table.setShowGrid(False)
        self.disk_table.setToolTip("Hücreleri seçip Ctrl+C ile kopyalayabilirsiniz.")
        self.disk_table.setMinimumHeight(130)
        self.disk_table.setMaximumHeight(240)
        disk_layout.addWidget(self.disk_table)

        self.device_details_section = CollapsibleSection(
            "Donanım ve İşletim Sistemi Ayrıntıları",
            "Ayrıntılı donanım ve Windows bilgilerini görmek için genişletin.",
            expanded=False,
        )
        details_layout = QVBoxLayout()
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(0)
        details_layout.addWidget(hw_shell)
        self.device_details_section.set_body_layout(details_layout)

        self.device_storage_section = CollapsibleSection(
            "Fiziksel Depolama",
            "Disk modeli, bağlantı türü, kapasite ve seri numarası için genişletin.",
            expanded=False,
        )
        storage_layout = QVBoxLayout()
        storage_layout.setContentsMargins(0, 0, 0, 0)
        storage_layout.setSpacing(0)
        storage_layout.addWidget(disk_shell)
        self.device_storage_section.set_body_layout(storage_layout)

        self.device_info_status_label = QLabel("Envanter okunuyor…")
        self.device_info_status_label.setObjectName("sectionSubtitle")
        layout.addLayout(title_row)
        layout.addWidget(summary_shell)
        layout.addWidget(self.device_details_section)
        layout.addWidget(self.device_storage_section)
        layout.addWidget(self.device_info_status_label)
        layout.addStretch(1)
        self.populate_device_info()

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    def populate_device_info(self) -> None:
        if not hasattr(self, "device_summary_values"):
            return
        self.device_info_refresh_button.setEnabled(False)
        try:
            inventory = self.service.get_system_inventory(lambda _message: None)
        except Exception as exc:  # noqa: BLE001
            inventory = {}
            self.device_info_status_label.setText(f"Envanter okunamadı: {exc}")

        def value(key: str, fallback: str = "Bilinmiyor") -> str:
            raw = inventory.get(key, fallback) if isinstance(inventory, dict) else fallback
            if raw is None:
                return fallback
            text = str(raw).strip()
            return text or fallback

        device_name = " ".join(part for part in (value("manufacturer", ""), value("model", "")) if part).strip()
        firmware = value("bios_summary")
        security = f"TPM: {value('tpm_summary')} | Secure Boot: {value('secure_boot_summary')}"
        summary_values = {
            "device": device_name or value("computer_name"),
            "serial": value("serial_number"),
            "processor": value("processor_name"),
            "memory": value("total_memory_display"),
            "system_disk": value("system_disk_summary"),
            "operating_system": value("os_summary"),
            "firmware": f"{value('firmware_type')} | {firmware}\n{security}",
            "graphics": value("gpu_summary"),
        }
        for key, label in self.device_summary_values.items():
            label.setText(summary_values.get(key, "Bilinmiyor"))

        detail_rows = [
            ("Bilgisayar adı", value("computer_name")),
            ("Üretici", value("manufacturer")),
            ("Model", value("model")),
            ("Seri no", value("serial_number")),
            ("Varlık etiketi", value("asset_tag")),
            ("Sistem UUID", value("system_uuid")),
            ("İşlemci", value("processor_name")),
            ("İşlemci topolojisi", value("processor_topology")),
            ("Bellek", f"{value('total_memory_display')} | {value('memory_summary')}"),
            ("Grafik", value("gpu_summary")),
            ("Anakart", value("motherboard_summary")),
            ("Anakart seri no", value("motherboard_serial")),
            ("BIOS", value("bios_summary")),
            ("Ürün yazılımı", value("firmware_type")),
            ("TPM", value("tpm_summary")),
            ("Secure Boot", value("secure_boot_summary")),
            ("İşletim sistemi", value("os_summary")),
        ]
        self.hw_table.clearContents()
        self.hw_table.setRowCount(len(detail_rows))
        for row, (label, detail) in enumerate(detail_rows):
            label_item = QTableWidgetItem(label)
            label_item.setForeground(QColor("#5d4a33"))
            self.hw_table.setItem(row, 0, label_item)
            self.hw_table.setItem(row, 1, QTableWidgetItem(detail))
            self.hw_table.setRowHeight(row, 32)

        disks = inventory.get("physical_disks", []) if isinstance(inventory, dict) else []
        if not isinstance(disks, list):
            disks = []
        self.disk_table.clearContents()
        self.disk_table.setRowCount(max(1, len(disks)))
        if disks:
            for row, disk in enumerate(disks):
                if not isinstance(disk, dict):
                    continue
                for column, key in enumerate(("name", "model", "interface", "size_display", "serial_number")):
                    self.disk_table.setItem(row, column, QTableWidgetItem(str(disk.get(key, "") or "Bilinmiyor")))
                self.disk_table.setRowHeight(row, 32)
        else:
            self.disk_table.setItem(0, 0, QTableWidgetItem("Disk bilgisi okunamadı"))
            self.disk_table.setRowHeight(0, 32)

        if isinstance(inventory, dict) and inventory.get("collection_status") == "complete":
            self.device_info_status_label.setText(
                f"Envanter güncellendi: {len(disks)} fiziksel disk | {datetime.now().strftime('%H:%M:%S')}"
            )
        elif not self.device_info_status_label.text().startswith("Envanter okunamadı"):
            self.device_info_status_label.setText("Envanter kısmi olarak okundu; bazı alanlar kullanılamıyor olabilir.")
        self.device_info_refresh_button.setEnabled(True)

    def copy_device_serial_number(self) -> None:
        serial = self.device_summary_values.get("serial", QLabel()).text().strip()
        if not serial or serial == "Bilinmiyor" or serial.startswith("Okunuyor"):
            self.device_info_status_label.setText("Kopyalanacak seri numarası henüz okunamadı.")
            return
        QApplication.clipboard().setText(serial)
        self.device_info_status_label.setText("Seri numarası panoya kopyalandı.")

    def build_main_area(self) -> QWidget:
        container = QWidget()
        container.setObjectName("mainArea")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        activity_layout = QGridLayout(self.activity_frame)
        activity_layout.setContentsMargins(16, 12, 16, 12)
        activity_layout.setHorizontalSpacing(14)
        activity_layout.setVerticalSpacing(6)
        activity_layout.addWidget(self.activity_title, 0, 0)
        activity_layout.addWidget(self.activity_elapsed, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
        activity_layout.addWidget(self.activity_detail, 1, 0, 1, 2)
        activity_layout.addWidget(self.activity_progress, 2, 0, 1, 2)

        compact_layout = QHBoxLayout(self.compact_toolbar)
        compact_layout.setContentsMargins(12, 8, 12, 8)
        compact_title = QLabel("AÇIK Kurulum")
        compact_title.setObjectName("miniTitle")
        compact_layout.addWidget(compact_title)
        compact_layout.addWidget(self.compact_preflight_status)
        compact_layout.addWidget(self.compact_step_status)
        compact_layout.addStretch(1)
        compact_layout.addWidget(self.compact_settings_button)

        self.main_tabs = QTabWidget()
        self.main_tabs.setObjectName("mainTabs")
        self.main_tabs.setDocumentMode(True)
        self.main_tabs.tabBar().setUsesScrollButtons(False)
        self.main_tabs.tabBar().setExpanding(True)
        self.main_tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.main_tabs.addTab(self.build_onboarding_tab(), "Kurulum")
        self.main_tabs.addTab(self.build_device_info_tab(), "Donanım Bilgileri")
        self.main_tabs.addTab(self.build_usb_util_tab(), "Program Kurulumu")
        self.main_tabs.addTab(self.build_backup_tab(), "Yedekleme")
        self.reports_tab = self.build_reports_tab()
        self.main_tabs.addTab(self.reports_tab, "Raporlar")

        layout.addWidget(self.compact_toolbar)
        layout.addWidget(self.activity_frame)
        layout.addWidget(self.main_tabs, 1)
        return container

    def build_onboarding_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("onboardingTab")
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(10)

        content = QWidget()
        content.setObjectName("tabSurface")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        layout.addWidget(self.build_active_workflow_card())
        layout.addWidget(self.build_summary_card())
        layout.addWidget(self.build_profile_card())
        layout.addWidget(self.build_identity_card())
        layout.addWidget(self.build_options_card())
        layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(content)
        self.onboarding_scroll_area = area
        self.sticky_actions_card = self.build_actions_card()
        self.sticky_actions_card.setObjectName("stickyActionsCard")
        tab_layout.addWidget(area, 1)
        tab_layout.addWidget(self.sticky_actions_card)
        return tab

    def build_active_workflow_card(self) -> QWidget:
        """Offer recovery actions before a stale workflow can block a new run."""
        card = QFrame()
        card.setObjectName("workflowRecoveryCard")
        self.workflow_recovery_card = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        title = QLabel("Onceki Kurulum Algilandi")
        title.setObjectName("workflowRecoveryTitle")
        self.workflow_recovery_title = title
        detail = QLabel()
        detail.setObjectName("workflowRecoveryDetail")
        detail.setWordWrap(True)
        self.workflow_recovery_detail = detail
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        actions.setSpacing(10)
        actions.addStretch(1)
        self.workflow_restart_button = QPushButton("Yeniden Baslat")
        self.workflow_restart_button.setObjectName("primaryButton")
        self.workflow_restart_button.clicked.connect(self.restart_active_workflow)
        self.workflow_continue_button = QPushButton("Surece Devam Et")
        self.workflow_continue_button.setObjectName("secondaryButton")
        self.workflow_continue_button.clicked.connect(self.continue_active_workflow)
        self.workflow_terminate_button = QPushButton("Sureci Sonlandir")
        self.workflow_terminate_button.setObjectName("primaryButton")
        self.workflow_terminate_button.clicked.connect(self.terminate_active_workflow)
        actions.addWidget(self.workflow_restart_button)
        actions.addWidget(self.workflow_continue_button)
        actions.addWidget(self.workflow_terminate_button)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addLayout(actions)
        card.hide()
        return card

    def refresh_active_workflow_card(self) -> bool:
        if not hasattr(self, "workflow_recovery_card"):
            return False
        summary = self.service.active_workflow_summary()
        if summary is None:
            self._automatic_workflow_recovery = False
            self.workflow_recovery_card.hide()
            return False

        status = summary.get("status", "bilinmiyor") or "bilinmiyor"
        run_id = summary.get("run_id", "") or "bilinmiyor"
        target = summary.get("target_username", "") or "bilinmiyor"
        target_type = summary.get("target_user_type", "") or "bilinmiyor"
        report_status = summary.get("report_status", "")
        detail = summary.get("detail", "")
        if detail:
            detail_text = detail
        else:
            report_text = f" | Rapor: {report_status}" if report_status else ""
            login_hint = (
                " Yerel hedef normal Windows kullanici secicisinde gorunur."
                if target_type.casefold() == "lokal"
                else ""
            )
            detail_text = (
                f"Run ID: {run_id} | Durum: {status}{report_text} | "
                f"Hedef kullanici: {target} ({target_type}). "
                "Surece Devam Et, hedef oturum aciksa kayitli ikinci faz gorevlerini baslatir; "
                "X temizligi secili ve eski oturum aciksa plan korunarak hedef kullaniciya gecis icin yeniden baslatir. "
                "Yeni kurulum baslatilmayacak."
                + login_hint
            )
        self.workflow_recovery_detail.setText(detail_text)
        automatic_only = status == "automatic"
        self._automatic_workflow_recovery = automatic_only
        if automatic_only:
            # A standard target account is intentionally denied the protected
            # SYSTEM state.  The old UI disabled every button here, which
            # looked like a broken card and gave the operator no safe route
            # back to the privileged recovery actions.
            self.workflow_recovery_title.setText("Yonetici Yetkisiyle Kurtarma Gerekli")
            self.workflow_recovery_detail.setText(
                detail_text
                + "\n\nDevam veya yeniden baslatma icin yonetici yetkisi gerekir. "
                "Asagidaki dugme UAC ile korumali kurtarma ekranini acar; "
                "bekleyen kurulum durumu korunur."
            )
            self.workflow_restart_button.setText("Yonetici ile Kurtarmayi Ac")
            self.workflow_restart_button.setEnabled(True)
            self.workflow_continue_button.hide()
            self.workflow_terminate_button.hide()
        else:
            self.workflow_recovery_title.setText("Onceki Kurulum Algilandi")
            self.workflow_restart_button.setText("Yeniden Baslat")
            self.workflow_restart_button.setEnabled(True)
            self.workflow_continue_button.setText("Surece Devam Et")
            self.workflow_continue_button.setEnabled(True)
            self.workflow_continue_button.show()
            self.workflow_terminate_button.setText("Sureci Sonlandir")
            self.workflow_terminate_button.setEnabled(True)
            self.workflow_terminate_button.show()
        self.workflow_recovery_card.show()
        return True

    def open_elevated_recovery(self) -> bool:
        """Open a separate elevated recovery UI without bypassing UAC.

        The normal target-user phase must remain unprivileged: it cannot read
        or alter the protected workflow state.  A distinct recovery mutex
        lets this window coexist with the standard-user window long enough for
        the operator to approve UAC and choose a real recovery action.
        """
        if os.name != "nt":
            self.show_message(
                "Kurtarma Acilamadi",
                "Yonetici kurtarma ekrani yalnizca Windows'ta acilabilir.",
                "warning",
            )
            return False

        # If this window is already elevated, never spawn another identical
        # process. Refreshing the protected-state ACL is enough to turn the
        # recovery card back into its real Continue/Restart controls.
        if self.service.is_admin_session():
            self.refresh_active_workflow_card()
            if not getattr(self, "_automatic_workflow_recovery", False):
                self.append_log("Yonetici oturumu dogrulandi; kurtarma kontrolleri yenilendi.")
                return True
            self.show_message(
                "Kurtarma Acilamadi",
                "Bu pencere zaten yonetici yetkisiyle acik; korumali is akisi ACL'i "
                "onarilamadi. Ayrintili hata kartta gosteriliyor.",
                "warning",
            )
            return False

        try:
            executable = str(Path(sys.executable).resolve())
            if getattr(sys, "frozen", False):
                parameters = "--recovery"
                working_dir = str(Path(executable).parent)
            else:
                root = Path(__file__).resolve().parents[2]
                parameters = subprocess.list2cmdline([str(root / "run_app.py"), "--recovery"])
                working_dir = str(root)
            result = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                executable,
                parameters,
                working_dir,
                1,
            )
        except Exception as exc:  # noqa: BLE001 - preserve a usable UI on UAC failures
            self.show_message(
                "Kurtarma Acilamadi",
                f"Yonetici kurtarma ekrani baslatilamadi: {exc}",
                "warning",
            )
            return False

        if result <= 32:
            self.show_message(
                "Yonetici Yetkisi Gerekli",
                "Kurtarma ekrani UAC onayi olmadan acilamaz. UAC isteminde yonetici "
                "hesabiyla onaylayin.",
                "warning",
            )
            return False
        self.append_log("Yonetici kurtarma ekrani UAC ile acildi.")
        return True

    def continue_active_workflow(self) -> None:
        """Resume the recorded target-bound phase instead of opening reports."""
        if not self.refresh_active_workflow_card():
            return
        self.run_background_task(
            lambda log: self.service.resume_active_workflow(log),
            self._on_active_workflow_continue_requested,
            "Bekleyen kurulum devam ettiriliyor",
        )

    def _on_active_workflow_continue_requested(self, messages: object) -> None:
        self.refresh_active_workflow_card()
        self.refresh_report_history()
        self.append_log("Bekleyen ikinci faz gorevleri hedef kullanici icin baslatildi.")
        if isinstance(messages, list):
            self.show_result_messages(messages)

    def restart_active_workflow(self) -> None:
        """Restart a pending plan without cancelling its protected state."""
        if not self.refresh_active_workflow_card():
            return
        if getattr(self, "_automatic_workflow_recovery", False):
            self.open_elevated_recovery()
            return
        if not self.ask_confirmation(
            "Bekleyen Kurulumu Yeniden Baslat",
            "Bekleyen post-login gorevleri korunacak. Bilgisayar hedef kullaniciya gecis icin "
            "yeniden baslatilacak; hedef oturumdan sonra SYSTEM X hesabini, profilini ve "
            "ProfileList kaydini dogrulayarak silecek. Devam edilsin mi?",
        ):
            return
        self.run_background_task(
            lambda log: self.service.restart_pending_workflow_for_handoff(log),
            self._on_active_workflow_restart_requested,
            "Bekleyen kurulum yeniden baslatiliyor",
        )

    def _on_active_workflow_restart_requested(self, target_username: object) -> None:
        target = str(target_username).strip() or "yeni kullanici"
        self.append_log(
            "Bekleyen kurulum korunuyor. Hedef kullaniciya gecis icin SYSTEM yeniden baslatma "
            "planlandi; hedef oturumdan sonra X temizligi dogrulanarak tamamlanacak."
        )
        self.show_message(
            "Hedef Kullaniciya Gecis Planlandi",
            f"Post-login gorevleri korunuyor. Bilgisayar hedef kullaniciya gecis icin yeniden "
            f"baslatilacak; bu oturumdan sonra SYSTEM X temizligini dogrulayacak. Hedef: {target}.",
            "info",
        )

    def terminate_active_workflow(self) -> None:
        if not self.refresh_active_workflow_card():
            return
        if not self.ask_confirmation(
            "Onceki Kurulumu Sonlandir",
            "Onceki kurulumun planlanan gorevleri ve durum dosyasi temizlenecek. "
            "Rapor silinmez, 'closed' olarak saklanir. Devam edilsin mi?",
        ):
            return
        self.run_background_task(
            lambda log: self.service.close_active_workflow_for_retry(log),
            self._on_active_workflow_terminated,
            "Onceki kurulum sonlandiriliyor",
        )

    def _on_active_workflow_terminated(self, _run_id: object) -> None:
        self.refresh_active_workflow_card()
        self.refresh_report_history()
        self.append_log("Onceki kurulum kapatildi. Yeni kurulum baslatabilirsiniz.")
        self.show_message(
            "Surec Sonlandirildi",
            "Eski kurulum temizlendi; yeni kurulum baslatabilirsiniz.",
            "info",
        )

    def build_actions_card(self) -> QWidget:
        card = QGroupBox("4. İşlemleri Başlat")
        layout = QGridLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        self.actions_layout = layout

        info_label = QLabel("Yukarıdaki adımları tamamladıktan sonra bilgileri üretip kurulumu başlatabilirsiniz.")
        info_label.setObjectName("sectionNote")
        info_label.setWordWrap(True)
        self.actions_info_label = info_label

        layout.addWidget(info_label, 0, 0, 1, 3)
        layout.addWidget(self.generate_button, 1, 0)
        layout.addWidget(self.create_button, 1, 1)
        layout.addWidget(self.terminate_button, 1, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)
        return card

    def build_summary_card(self) -> QWidget:
        card = QGroupBox("Kurulum Özeti")
        layout = QGridLayout(card)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)
        self.summary_grid = layout

        summary_rows = [
            ("Seçili Profil", self.summary_profile_value),
            ("Kullanıcı Tipi", self.summary_user_type_value),
            ("Şirket", self.summary_company_value),
            ("Kullanıcı Adı", self.summary_username_value),
            ("PC Adı", self.summary_pc_value),
            ("Açık Adım", self.summary_steps_value),
        ]
        for index, (label, value_label) in enumerate(summary_rows):
            value_label.setObjectName("summaryValue")
            value_label.setWordWrap(True)
            tile = self.build_summary_tile(label, value_label)
            self.summary_tiles.append(tile)
            layout.addWidget(tile, index // 3, index % 3)
        return card

    def build_summary_tile(
        self,
        title: str,
        value_label: QLabel,
        accessory: QWidget | None = None,
    ) -> QWidget:
        frame = QFrame()
        frame.setObjectName("summaryTile")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setObjectName("summaryTitle")
        title_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        value_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title_row.addWidget(title_label, 1)
        if accessory is not None:
            title_row.addWidget(accessory, 0, Qt.AlignmentFlag.AlignRight)

        layout.addLayout(title_row)
        layout.addWidget(value_label)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return frame

    def build_profile_card(self) -> QWidget:
        card = QGroupBox("1. Kurulum Profili ve Ön Kontrol")
        layout = QVBoxLayout(card)
        layout.setSpacing(10)

        self.profile_actions_grid = QGridLayout()
        self.profile_actions_grid.setHorizontalSpacing(12)
        self.profile_actions_grid.setVerticalSpacing(10)
        self.profile_profile_field = self.field_block("Hazır Profil", self.profile_combo)
        self.apply_profile_button.setFixedHeight(46)
        self.preflight_button.setFixedHeight(46)
        self.profile_actions_grid.addWidget(self.profile_profile_field, 0, 0)
        self.profile_actions_grid.addWidget(self.apply_profile_button, 0, 1)
        self.profile_actions_grid.addWidget(self.preflight_button, 0, 2)
        self.profile_actions_grid.setColumnStretch(0, 1)

        self.profile_note.setObjectName("profileNote")
        self.profile_note.setWordWrap(True)

        layout.addLayout(self.profile_actions_grid)
        layout.addWidget(self.profile_note)
        return card

    def build_preflight_card(self, compact: bool = False) -> QWidget:
        card = QGroupBox("Ön Kontrol")
        if compact:
            card.setObjectName("sidebarInfoCard")
        layout = QVBoxLayout(card)
        layout.setSpacing(8 if compact else 10)

        self.preflight_status_label.setObjectName("sidebarSectionNote" if compact else "sectionNote")
        self.preflight_status_label.setWordWrap(True)
        if compact:
            self.preflight_table.setMinimumHeight(180)
            self.preflight_table.setMaximumHeight(220)

        layout.addWidget(self.preflight_status_label)
        layout.addWidget(self.preflight_table, 1)
        return card

    def build_step_tracker_card(self, compact: bool = False) -> QWidget:
        card = QGroupBox("Adım Durumu")
        if compact:
            card.setObjectName("sidebarInfoCard")
        layout = QVBoxLayout(card)
        layout.setSpacing(8 if compact else 10)

        self.step_hint_label.setObjectName("sidebarSectionNote" if compact else "sectionNote")
        self.step_hint_label.setWordWrap(True)
        if compact:
            self.step_table.setMinimumHeight(210)
            self.step_table.setMaximumHeight(280)

        layout.addWidget(self.step_hint_label)
        layout.addWidget(self.step_table, 1)
        return card

    def build_identity_card(self) -> QWidget:
        card = QGroupBox("2. Kimlik ve Bilgisayar Bilgileri")
        grid = QGridLayout(card)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(14)
        self.identity_grid = grid

        self.full_name_input.setPlaceholderText("Örn: Özay Bölüt")
        self.username_output.setPlaceholderText("Otomatik üretilecek")
        self.pc_name_output.setPlaceholderText("Otomatik üretilecek")
        self.password_output.setPlaceholderText("Şirket şifresi")
        password_container = QFrame()
        password_container_layout = QVBoxLayout(password_container)
        password_container_layout.setContentsMargins(0, 0, 0, 0)
        password_container_layout.setSpacing(5)
        password_row = QFrame()
        password_row_layout = QHBoxLayout(password_row)
        password_row_layout.setContentsMargins(0, 0, 0, 0)
        password_row_layout.setSpacing(8)
        password_toggle = QPushButton("Göster")
        password_toggle.setCheckable(True)
        password_toggle.setObjectName("secondaryButton")
        password_toggle.setMinimumWidth(82)
        password_toggle.toggled.connect(
            lambda visible: (
                self.password_output.setEchoMode(
                    QLineEdit.EchoMode.Normal
                    if visible
                    else QLineEdit.EchoMode.Password
                ),
                password_toggle.setText("Gizle" if visible else "Göster"),
            )
        )
        password_row_layout.addWidget(self.password_output, 1)
        password_row_layout.addWidget(password_toggle)
        password_container_layout.addWidget(password_row)
        password_container_layout.addWidget(self.company_password_status)
        password_container_layout.addWidget(self.domain_signin_hint)

        self.identity_fields = [
            self.field_block("Kullanıcı Tipi", self.user_type_combo),
            self.field_block("Şirket", self.company_combo),
            self.field_block("Ad Soyad", self.full_name_input),
            self.field_block("Kullanıcı Adı", self.username_output),
            self.field_block("PC Adı", self.pc_name_output),
            self.field_block("Şifre", password_container),
        ]
        grid.addWidget(self.identity_fields[0], 0, 0)
        grid.addWidget(self.identity_fields[1], 0, 1)
        grid.addWidget(self.identity_fields[2], 1, 0, 1, 2)
        grid.addWidget(self.identity_fields[3], 2, 0)
        grid.addWidget(self.identity_fields[4], 2, 1)
        grid.addWidget(self.identity_fields[5], 3, 0, 1, 2)
        return card

    def field_block(self, label: str, widget: QWidget) -> QWidget:
        wrapper = QFrame()
        wrapper.setObjectName("fieldBlock")
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        caption.setBuddy(widget)

        if isinstance(widget, (QLineEdit, QComboBox)):
            widget.setFixedHeight(46)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(caption)
        layout.addWidget(widget)
        return wrapper

    def build_options_card(self) -> QWidget:
        card = QGroupBox("3. Kurulum Adım Seçenekleri")
        outer = QVBoxLayout(card)
        outer.setSpacing(14)

        groups = QGridLayout()
        groups.setHorizontalSpacing(14)
        groups.setVerticalSpacing(14)
        groups.setColumnStretch(0, 1)
        groups.setColumnStretch(1, 1)
        self.options_group_grid = groups
        self.option_group_widgets = []

        for index, (title, subtitle, options) in enumerate(OPTION_GROUPS):
            widget = self.option_group(title, subtitle, options, expanded=True)
            self.option_group_widgets.append(widget)
            groups.addWidget(widget, index // 2, index % 2)

        note = QLabel("Açık durumdaki satırlar uygulanır. File Server, ağ yazıcısı, imza ve Outlook adımları yeni kullanıcı oturumunda Mikrolink_Ofis bağlantısı üzerinden ikinci faz olarak tamamlanır.")
        note.setObjectName("sectionNote")
        note.setWordWrap(True)

        outer.addLayout(groups)
        outer.addWidget(note)
        return card

    def option_group(
        self,
        title: str,
        subtitle: str,
        options: list[tuple[str, str, str, bool]],
        expanded: bool = True,
    ) -> QWidget:
        box = CollapsibleSection(title, subtitle, expanded=expanded)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        for name, text, description, checked in options:
            layout.addWidget(self.build_action_row(name, text, description, checked))

        if title == "Kurulum ve Sistem":
            self.system_options_section = box
            recovery_row = QFrame()
            recovery_row.setObjectName("domainRecoveryRow")
            recovery_layout = QHBoxLayout(recovery_row)
            recovery_layout.setContentsMargins(14, 12, 14, 12)
            recovery_layout.setSpacing(14)
            recovery_text = QVBoxLayout()
            recovery_text.setSpacing(4)
            recovery_title = QLabel("Eski Domain Kurtarma")
            recovery_title.setObjectName("actionTitle")
            recovery_detail = QLabel(
                "Yalnız test nedeniyle domaine kalmış cihazı kurulum başlatmadan workgroup'a döndürür."
            )
            recovery_detail.setObjectName("actionDescription")
            recovery_detail.setWordWrap(True)
            recovery_text.addWidget(recovery_title)
            recovery_text.addWidget(recovery_detail)
            self.domain_leave_button.setToolTip(
                "Kurulum başlatmadan kayıtlı domain yetkisiyle cihazı domain üyeliğinden çıkarır."
            )
            recovery_layout.addLayout(recovery_text, 1)
            recovery_layout.addWidget(self.domain_leave_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(recovery_row)

        layout.addStretch(1)
        box.set_body_layout(layout)
        self.option_sections.append(box)
        return box

    def build_action_row(self, name: str, title: str, description: str, checked: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("actionRow")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)

        text_column = QVBoxLayout()
        text_column.setSpacing(4)

        title_label = QLabel(title)
        title_label.setObjectName("actionTitle")
        self.action_title_labels[name] = title_label
        description_label = QLabel(description)
        description_label.setObjectName("actionDescription")
        description_label.setWordWrap(True)

        text_column.addWidget(title_label)
        text_column.addWidget(description_label)

        if name == "desktop_wallpaper":
            # Varsayılan olarak USB paketinin assets klasöründeki görselleri kullan.
            current_source = self.config.desktop_automation.wallpaper_source_path
            assets_fallback = self.config.base_dir / "assets" / "wallpaper.jpg"
            if not current_source or not Path(str(current_source)).exists() or Path(str(current_source)).stat().st_size == 0:
                if assets_fallback.exists() and assets_fallback.stat().st_size > 0:
                    self.config.desktop_automation.wallpaper_source_path = assets_fallback
                    current_source = assets_fallback

            display_name = Path(str(current_source)).name if current_source and Path(str(current_source)).exists() else "Seçilmedi"
            self.wallpaper_path_label = QLabel(f"🖥️ Masaüstü: {display_name}")
            self.wallpaper_path_label.setStyleSheet("color: #8f7238; font-weight: bold; font-size: 12px;")
            self.wallpaper_path_label.setWordWrap(True)

            path_row = QHBoxLayout()
            path_row.setSpacing(6)
            browse_btn = QPushButton("Masaüstü Seç")
            browse_btn.setFixedWidth(112)
            browse_btn.setMinimumHeight(40)
            browse_btn.setStyleSheet("font-size: 11px; padding: 2px 6px; border-radius: 8px; background-color: #f3ead7; color: #34281d; border: 1px solid #decda9;")
            browse_btn.clicked.connect(self.pick_wallpaper_file)
            path_row.addWidget(self.wallpaper_path_label, 1)
            path_row.addWidget(browse_btn)
            text_column.addLayout(path_row)

        toggle = QPushButton("Açık" if checked else "Kapalı")
        toggle.setCheckable(True)
        toggle.setChecked(checked)
        toggle.setObjectName("actionToggle")
        toggle.setMinimumWidth(92)
        toggle.setMinimumHeight(42)
        toggle.setAccessibleName(f"{title}: {'Açık' if checked else 'Kapalı'}")
        toggle.setAccessibleDescription(description)
        toggle.clicked.connect(lambda state, button=toggle, option_name=name: self.on_option_toggled(option_name, button, state))

        self.checkboxes[name] = toggle

        layout.addLayout(text_column, 1)
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def pick_wallpaper_file(self) -> None:
        if not self.request_settings_access("duvar kağıdını değiştirmek"):
            return
        start_dir = str(self.config.base_dir / "assets")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Duvar Kağıdı Görseli Seç",
            start_dir,
            "Resim Dosyaları (*.jpg *.jpeg *.png *.bmp)"
        )
        if file_path:
            selected = Path(file_path).resolve()
            self.config.desktop_automation.wallpaper_source_path = selected
            self.config.desktop_automation.wallpaper_target_path = selected
            self.wallpaper_path_label.setText(f"🖥️ Masaüstü: {selected.name}")
            save_app_config(self.config)
            self.append_log(f"Yeni duvar kağıdı seçildi: {file_path}")
            self.refresh_option_states()

    def pick_lock_screen_file(self) -> None:
        if not self.request_settings_access("kilit ekranı görselini değiştirmek"):
            return
        start_dir = str(self.config.base_dir / "assets")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Kilit Ekranı Görseli Seç",
            start_dir,
            "Resim Dosyaları (*.jpg *.jpeg *.png *.bmp)",
        )
        if file_path:
            selected = Path(file_path).resolve()
            self.config.desktop_automation.lock_screen_source_path = selected
            self.config.desktop_automation.lock_screen_target_path = selected
            self.lock_screen_path_label.setText(f"🔒 Kilit ekranı: {selected.name}")
            save_app_config(self.config)
            self.append_log(f"Yeni kilit ekranı görseli seçildi: {file_path}")
            self.refresh_option_states()

    def build_backup_tab(self) -> QWidget:
        content = QWidget()
        content.setObjectName("tabSurface")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(16)

        card = QGroupBox("Profil Yedekleme")
        form = QVBoxLayout(card)
        form.setSpacing(14)

        # Source folder selection
        browse_source_button = QPushButton("Gözat")
        browse_source_button.clicked.connect(self.pick_backup_source)

        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        source_row.addWidget(self.backup_source_input, 1)
        source_row.addWidget(browse_source_button)

        # Destination folder selection
        browse_dest_button = QPushButton("Gözat")
        browse_dest_button.clicked.connect(self.pick_destination)

        destination_row = QHBoxLayout()
        destination_row.setSpacing(10)
        destination_row.addWidget(self.destination_input, 1)
        destination_row.addWidget(browse_dest_button)

        self.backup_user_combo.currentTextChanged.connect(self.on_backup_user_combo_changed)

        form.addWidget(self.field_block("Kullanıcı Profili (Otomatik doldurmak için)", self.backup_user_combo))
        form.addWidget(self.field_block("Kaynak Klasör (Kopyalanacak)", self.wrap_layout(source_row)))
        form.addWidget(self.field_block("Hedef Klasör (Yedek yeri)", self.wrap_layout(destination_row)))

        note = QLabel("Seçilen kaynak klasör altındaki masaüstü, belgeler, resimler ve videolar klasörleri hedef klasöre yedeklenir. Kaynak ve hedef klasörleri doğrudan seçebilirsiniz.")
        note.setObjectName("sectionNote")
        note.setWordWrap(True)

        layout.addWidget(card)
        layout.addWidget(note)
        layout.addWidget(self.backup_button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    def build_reports_tab(self) -> QWidget:
        content = QWidget()
        content.setObjectName("tabSurface")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(16)

        header = QFrame()
        header.setObjectName("miniToolbar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 10, 12, 10)
        header_layout.setSpacing(10)
        self.report_summary_label.setObjectName("settingsNote")
        self.report_refresh_button.clicked.connect(self.refresh_report_history)
        self.report_delete_button.clicked.connect(self.delete_all_reports)
        self.usb_diagnostics_button = QPushButton("USB Test Hataları")
        self.usb_diagnostics_button.setObjectName("secondaryButton")
        self.usb_diagnostics_button.clicked.connect(self.open_usb_diagnostics_dialog)
        header_layout.addWidget(self.report_summary_label, 1)
        header_layout.addWidget(self.usb_diagnostics_button)
        header_layout.addWidget(self.report_delete_button)
        header_layout.addWidget(self.report_refresh_button)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        self.report_splitter = splitter

        table_shell = QFrame()
        table_layout = QVBoxLayout(table_shell)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(10)
        table_layout.addWidget(self.report_table)

        detail_shell = QGroupBox("Rapor Detayı")
        detail_layout = QVBoxLayout(detail_shell)
        detail_layout.setSpacing(10)
        detail_layout.addWidget(self.report_detail)

        splitter.addWidget(table_shell)
        splitter.addWidget(detail_shell)
        splitter.setSizes([200, 450])

        layout.addWidget(header)
        layout.addWidget(splitter, 1)
        return content

    def open_usb_diagnostics_dialog(self) -> None:
        """Keep removable-drive diagnostics available without shrinking reports."""
        dialog = QDialog(self)
        dialog.setWindowTitle("USB Test Cihazı Hata Raporları")
        dialog.setModal(True)
        dialog.resize(760, 560)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.build_usb_diagnostics_card(), 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.refresh_usb_diagnostics()
        dialog.exec()

    def build_format_tab(self) -> QWidget:
        content = QWidget()
        content.setObjectName("tabSurface")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(16)

        info_card = QGroupBox("Windows Format İşlemi")
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(16, 16, 16, 16)
        info_layout.setSpacing(10)

        warning_lbl = QLabel(
            "<html><body>"
            "<h3 style='color:#d32f2f;'>DİKKAT: BİLGİSAYARA FORMAT ATILACAK</h3>"
            "<p>Bu işlem, bilgisayarınızı Gelişmiş Başlangıç (Advanced Startup) seçenekleriyle yeniden başlatır.</p>"
            "<p>Yeniden başlatma sonrasında <b>'Bir aygıt kullan'</b> seçeneğinden Ventoy USB belleğinizi seçerek format işlemine başlayabilirsiniz.</p>"
            "<p>Lütfen devam etmeden önce tüm önemli verilerinizin yedeğini aldığınızdan emin olun.</p>"
            "</body></html>"
        )
        warning_lbl.setWordWrap(True)

        self.reboot_format_btn = QPushButton("Bilgisayarı Yeniden Başlat ve Formatı Başlat")
        self.reboot_format_btn.setObjectName("primaryButton")
        self.reboot_format_btn.setStyleSheet("background-color: #d32f2f; color: white; font-weight: bold; padding: 10px;")
        self.reboot_format_btn.clicked.connect(self.prompt_reboot_for_format)

        info_layout.addWidget(warning_lbl)
        info_layout.addWidget(self.reboot_format_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(info_card)
        layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)

        return area

    def prompt_reboot_for_format(self) -> None:
        class FormatConfirmDialog(QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Format Öncesi Son Kontroller")
                self.setModal(True)
                
                layout = QVBoxLayout(self)
                layout.setSpacing(10)
                
                lbl = QLabel("<b>Lütfen aşağıdaki adımların tamamlandığından emin olun:</b><br>")
                layout.addWidget(lbl)
                
                self.cb_backup = QCheckBox("Yedekleme işlemi yapıldı (veya gerekli değil)")
                self.cb_profile = QCheckBox("Profil ve veri taşıma adımları tamamlandı")
                self.cb_usb = QCheckBox("Ventoy USB belleği takılı ve hazır")
                
                for cb in (self.cb_backup, self.cb_profile, self.cb_usb):
                    layout.addWidget(cb)
                    cb.toggled.connect(self.check_ready)
                
                layout.addSpacing(10)
                
                btn_layout = QHBoxLayout()
                self.btn_cancel = QPushButton("İptal")
                self.btn_cancel.clicked.connect(self.reject)
                
                self.btn_confirm = QPushButton("Eminim, Yeniden Başlat")
                self.btn_confirm.setObjectName("primaryButton")
                self.btn_confirm.setStyleSheet("background-color: #d32f2f; color: white; font-weight: bold;")
                self.btn_confirm.setEnabled(False)
                self.btn_confirm.clicked.connect(self.accept)
                
                btn_layout.addWidget(self.btn_cancel)
                btn_layout.addWidget(self.btn_confirm)
                
                layout.addLayout(btn_layout)
                
            def check_ready(self):
                is_ready = self.cb_backup.isChecked() and self.cb_profile.isChecked() and self.cb_usb.isChecked()
                self.btn_confirm.setEnabled(is_ready)

        dialog = FormatConfirmDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.reboot_for_format()


    def build_usb_util_tab(self) -> QWidget:
        """Build the program-installation page without diagnostic history."""
        content = QWidget()
        content.setObjectName("tabSurface")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(16)

        status_card = QGroupBox("USB Bağlantı Durumu")
        status_layout = QHBoxLayout(status_card)
        status_layout.setContentsMargins(16, 16, 16, 16)
        status_layout.setSpacing(12)
        self.usb_status_label = QLabel("USB Taranıyor...")
        self.usb_status_label.setObjectName("sectionNote")
        self.usb_refresh_button = QPushButton("Yenile / USB Tara")
        self.usb_refresh_button.setObjectName("secondaryButton")
        self.usb_refresh_button.clicked.connect(self.scan_usb_util_path)
        status_layout.addWidget(self.usb_status_label, 1)
        status_layout.addWidget(self.usb_refresh_button)

        programs_card = QGroupBox("Kurulacak Programlar")
        programs_layout = QVBoxLayout(programs_card)
        programs_layout.setContentsMargins(16, 16, 16, 16)
        programs_layout.setSpacing(12)
        self.usb_checkboxes = {}
        self.usb_status_labels = {}
        self.usb_install_buttons = {}
        self.usb_connect_buttons = {}
        program_items = [
            ("eset", "ESET PROTECT Installer (Zorunlu)"),
            ("anydesk", "AnyDesk (Zorunlu)"),
            ("chrome", "Google Chrome (İsteğe Bağlı)"),
            ("forticlient", "FortiClient VPN Kurulum (İsteğe Bağlı)"),
            ("office", "Microsoft Office (İsteğe Bağlı)"),
            ("jre", "Java Runtime Environment (İsteğe Bağlı)"),
            ("winrar", "WinRAR (İsteğe Bağlı)"),
            ("hackbgrt", "HackBGRT (İsteğe Bağlı)"),
        ]
        for key, display_name in program_items:
            row = QFrame()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 4, 4, 4)
            row_layout.setSpacing(10)
            checkbox = QCheckBox(display_name)
            if key in ("eset", "anydesk"):
                checkbox.setChecked(True)
                checkbox.setEnabled(False)
            self.usb_checkboxes[key] = checkbox
            status_label = QLabel("Taranıyor...")
            status_label.setStyleSheet("color: #777; font-size: 12px; font-weight: bold;")
            self.usb_status_labels[key] = status_label
            install_button = QPushButton("Kur")
            install_button.setFixedWidth(64)
            install_button.setFixedHeight(26)
            install_button.setStyleSheet(
                "font-size: 11px; padding: 2px 4px; border-radius: 6px; "
                "background-color: #efdfba; color: #1f1b18; border: 1px solid #d4c19a;"
            )
            install_button.clicked.connect(
                lambda checked=False, program_name=key: self.install_single_usb_program(program_name)
            )
            self.usb_install_buttons[key] = install_button
            if key == "forticlient":
                connect_button = QPushButton("Bağlan")
                connect_button.setFixedWidth(64)
                connect_button.setFixedHeight(26)
                connect_button.setEnabled(False)
                connect_button.setToolTip("MKR_FC_RA VPN bağlantısını FortiClient komutuyla başlatır.")
                connect_button.setStyleSheet(
                    "font-size: 11px; padding: 2px 4px; border-radius: 6px; "
                    "background-color: #d8e8f7; color: #15324b; border: 1px solid #a8c5dd;"
                )
                connect_button.clicked.connect(self.connect_forticlient)
                self.usb_connect_buttons[key] = connect_button
            row_layout.addWidget(checkbox, 1)
            row_layout.addWidget(status_label)
            if key == "forticlient":
                row_layout.addWidget(self.usb_connect_buttons[key])
            row_layout.addWidget(install_button)
            programs_layout.addWidget(row)

        self.usb_install_button = QPushButton("Seçilen Kurulumları Başlat")
        self.usb_install_button.setObjectName("primaryButton")
        self.usb_install_button.clicked.connect(self.start_usb_installations)
        layout.addWidget(status_card)
        layout.addWidget(programs_card)
        layout.addWidget(self.usb_install_button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    def scan_usb_util_path(self) -> None:
        self.detected_usb_path = self.service.detect_usb_util_path()
        if self.detected_usb_path:
            self.usb_status_label.setText(
                f"<html><body><span style='color:#2e7d32; font-weight:bold;'>✔ Vent1 USB Algılandı</span><br/>"
                f"<span style='color:#555;'>Konum: {self.detected_usb_path}</span></body></html>"
            )
        else:
            self.usb_status_label.setText(
                "<html><body><span style='color:#e65100; font-weight:bold;'>⚠ Vent1 USB Algılanamadı</span><br/>"
                "<span style='color:#555;'>Yüklemeleri başlatmak için butona basıp kurulum klasörünü el ile seçebilirsiniz.</span></body></html>"
            )
        # Buton her zaman tıklanabilir kalarak manuel klasör seçimine olanak tanır.
        self.usb_install_button.setEnabled(True)
        self.refresh_usb_diagnostics()

    def refresh_usb_diagnostics(self) -> None:
        """Show only validated, redacted diagnostics from removable USBs."""
        if not hasattr(self, "usb_diagnostic_output"):
            return
        records = self.service.load_usb_diagnostics()
        self.usb_diagnostic_output.setPlainText(
            self.service.format_usb_diagnostic_report(records)
        )
        if records:
            self.usb_diagnostic_status.setText(
                f"{len(records)} hata kaydi bulundu. En yeni kayit ustte listelenir."
            )
            self.usb_diagnostic_status.setStyleSheet(
                "color: #a75f00; font-weight: 700;"
            )
        else:
            self.usb_diagnostic_status.setText(
                "Bagli USB bellekte incelenecek hata kaydi yok."
            )
            self.usb_diagnostic_status.setStyleSheet("color: #5f6a56;")

    def start_usb_installations(self) -> None:
        if not hasattr(self, "detected_usb_path") or not self.detected_usb_path:
            from PySide6.QtWidgets import QFileDialog
            selected_dir = QFileDialog.getExistingDirectory(
                self,
                "Lütfen '1.UTIL_KURULUM' Klasörünü Seçin",
                str(self.config.base_dir)
            )
            if selected_dir:
                self.detected_usb_path = Path(selected_dir)
                self.usb_status_label.setText(
                    f"<html><body><span style='color:#2e7d32; font-weight:bold;'>✔ Manuel Konum Seçildi</span><br/>"
                    f"<span style='color:#555;'>Konum: {self.detected_usb_path}</span></body></html>"
                )
            else:
                self.show_message("Hata", "Kurulum klasörü seçilmediği için işlem başlatılamaz.", "warning")
                return

        selected = {name: cb.isChecked() for name, cb in self.usb_checkboxes.items()}
        self.append_log("Program kurulum akışı başlatıldı.")
        
        self.run_background_task(
            lambda log: self.service.run_usb_util_installations(self.detected_usb_path, selected, log),
            self.show_result_messages,
        )

    def install_single_usb_program(self, program_name: str) -> None:
        if not hasattr(self, "detected_usb_path") or not self.detected_usb_path:
            from PySide6.QtWidgets import QFileDialog
            selected_dir = QFileDialog.getExistingDirectory(
                self,
                "Lütfen '1.UTIL_KURULUM' Klasörünü Seçin",
                str(self.config.base_dir)
            )
            if selected_dir:
                self.detected_usb_path = Path(selected_dir)
                self.usb_status_label.setText(
                    f"<html><body><span style='color:#2e7d32; font-weight:bold;'>✔ Manuel Konum Seçildi</span><br/>"
                    f"<span style='color:#555;'>Konum: {self.detected_usb_path}</span></body></html>"
                )
            else:
                self.show_message("Hata", "Kurulum klasörü seçilmediği için işlem başlatılamaz.", "warning")
                return

        self.append_log(f"{program_name} tekli program kurulum akışı başlatıldı.")
        selected = {name: (name == program_name) for name in self.usb_checkboxes.keys()}
        
        self.run_background_task(
            lambda log: self.service.run_usb_util_installations(self.detected_usb_path, selected, log),
            self.show_result_messages,
        )

    def connect_forticlient(self) -> None:
        """Connect the existing FortiClient tunnel without blocking the UI."""
        self.append_log("FortiClient VPN otomatik bağlantısı başlatıldı.")
        self.run_background_task(
            lambda log: self.service.connect_forticlient(log),
            self.show_forticlient_connection_result,
            task_name="FortiClient VPN bağlantısı",
        )

    def show_forticlient_connection_result(self, messages: object) -> None:
        if not isinstance(messages, list):
            return
        for title, text, level in messages:
            self.show_message(title, text, level)

    def build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("logShell")
        self.log_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Canlı İşlem Günlüğü")
        title.setObjectName("logTitle")
        self.log_title = title
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(title)
        title_row.addStretch(1)
        subtitle = QLabel("Kurulum sırasında çalışan her adım, gelen çıktı ve olası hata burada görünür.")
        subtitle.setObjectName("logSubtitle")
        subtitle.setWordWrap(True)

        filter_shell = QFrame()
        filter_shell.setObjectName("logFilterShell")
        self.log_filter_shell = filter_shell
        filter_grid = QGridLayout(filter_shell)
        filter_grid.setContentsMargins(0, 0, 0, 0)
        filter_grid.setHorizontalSpacing(8)
        filter_grid.setVerticalSpacing(8)
        self.log_filter_grid = filter_grid
        for filter_name, label in [
            ("all", "Tümü"),
            ("important", "Önemli"),
            ("info", "Bilgi"),
            ("success", "Başarılı"),
            ("warning", "Uyarı"),
            ("error", "Hata"),
        ]:
            button = QPushButton(label)
            button.setObjectName("logFilterButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked, key=filter_name: self.set_log_filter(key))
            self.log_filter_buttons[filter_name] = button
        self.log_filter_buttons["all"].setChecked(True)
        self.refresh_log_filter_layout()

        log_surface = QFrame()
        log_surface.setObjectName("logSurface")
        log_layout = QVBoxLayout(log_surface)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(0)

        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Henüz işlem başlamadı.")

        log_layout.addWidget(self.log_output)
        layout.addLayout(title_row)
        layout.addWidget(subtitle)
        layout.addWidget(filter_shell)
        layout.addWidget(log_surface, 1)
        return panel

    def wrap_layout(self, layout: QHBoxLayout) -> QWidget:
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _clear_layout(self, layout: QGridLayout | QHBoxLayout | QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            if child_layout is not None:
                self._clear_layout(child_layout)

    def _reflow_grid(self, layout: QGridLayout | None, widgets: list[QWidget], columns: int) -> None:
        if layout is None or not widgets:
            return
        columns = max(1, columns)
        self._clear_layout(layout)
        for index, widget in enumerate(widgets):
            layout.addWidget(widget, index // columns, index % columns)
        for column in range(max(4, columns)):
            layout.setColumnStretch(column, 1 if column < columns else 0)

    def refresh_log_filter_layout(self) -> None:
        if self.log_filter_grid is None or not self.log_filter_buttons:
            return
        self._clear_layout(self.log_filter_grid)
        # Keep the filter controls compact.  The destructive log-only action
        # sits immediately beside Hata, where an operator expects it, rather
        # than consuming the live-log title row.
        button_order = ["all", "important", "info", "success", "warning"]
        available_width = self.log_panel.width() if self.log_panel is not None else 0
        columns = 3 if available_width >= 520 else 2
        for index, key in enumerate(button_order):
            if key not in self.log_filter_buttons:
                continue
            button = self.log_filter_buttons[key]
            button.setMinimumHeight(40)
            button.setMinimumWidth(0)
            self.log_filter_grid.addWidget(button, index // columns, index % columns)
        action_row = (len(button_order) + columns - 1) // columns
        error_button = self.log_filter_buttons.get("error")
        if error_button is not None:
            error_button.setMinimumHeight(40)
            error_button.setMinimumWidth(0)
            self.log_filter_grid.addWidget(error_button, action_row, 0)
        self.log_filter_grid.addWidget(self.clear_log_button, action_row, 1)
        for column in range(3):
            self.log_filter_grid.setColumnStretch(column, 1 if column < columns else 0)

    def _refresh_profile_layout(self, mode: str) -> None:
        if self.profile_actions_grid is None or self.profile_profile_field is None:
            return
        self._clear_layout(self.profile_actions_grid)
        for column in range(3):
            self.profile_actions_grid.setColumnStretch(column, 0)

        if mode == "wide":
            self.profile_actions_grid.addWidget(self.preflight_button, 0, 0)
            self.profile_actions_grid.addWidget(self.profile_profile_field, 0, 1)
            self.profile_actions_grid.addWidget(self.apply_profile_button, 0, 2)
            self.profile_actions_grid.setColumnStretch(1, 1)
        elif mode == "medium":
            self.profile_actions_grid.addWidget(self.profile_profile_field, 0, 0, 1, 2)
            self.profile_actions_grid.addWidget(self.preflight_button, 1, 0)
            self.profile_actions_grid.addWidget(self.apply_profile_button, 1, 1)
            self.profile_actions_grid.setColumnStretch(0, 1)
            self.profile_actions_grid.setColumnStretch(1, 1)
        else:
            self.profile_actions_grid.addWidget(self.profile_profile_field, 0, 0)
            self.profile_actions_grid.addWidget(self.preflight_button, 1, 0)
            self.profile_actions_grid.addWidget(self.apply_profile_button, 2, 0)
            self.profile_actions_grid.setColumnStretch(0, 1)

    def _refresh_identity_layout(self, mode: str) -> None:
        if self.identity_grid is None or len(self.identity_fields) < 6:
            return
        self._clear_layout(self.identity_grid)
        for column in range(3):
            self.identity_grid.setColumnStretch(column, 0)

        if mode == "compact":
            for row, widget in enumerate(self.identity_fields):
                self.identity_grid.addWidget(widget, row, 0)
            self.identity_grid.setColumnStretch(0, 1)
            return

        self.identity_grid.addWidget(self.identity_fields[0], 0, 0)
        self.identity_grid.addWidget(self.identity_fields[1], 0, 1)
        self.identity_grid.addWidget(self.identity_fields[2], 1, 0, 1, 2)
        self.identity_grid.addWidget(self.identity_fields[3], 2, 0)
        self.identity_grid.addWidget(self.identity_fields[4], 2, 1)
        self.identity_grid.addWidget(self.identity_fields[5], 3, 0, 1, 2)
        self.identity_grid.setColumnStretch(0, 1)
        self.identity_grid.setColumnStretch(1, 1)

    def _refresh_report_layout(self, mode: str) -> None:
        if self.report_splitter is None:
            return
        self.report_splitter.setOrientation(Qt.Orientation.Vertical)
        if mode == "compact":
            self.report_splitter.setSizes([200, 380])
        else:
            self.report_splitter.setSizes([220, 420])

    def _refresh_actions_layout(self, mode: str) -> None:
        if not hasattr(self, "actions_layout") or self.actions_layout is None:
            return
        self._clear_layout(self.actions_layout)
        # The action area stays a compact two-column grid at every window
        # size.  This keeps the primary workflow visible without a tall stack
        # of buttons on small laptops.
        self.actions_layout.addWidget(self.actions_info_label, 0, 0, 1, 3)
        for index, button in enumerate(
            (
                self.generate_button,
                self.create_button,
                self.terminate_button,
            )
        ):
            self.actions_layout.addWidget(button, 1, index)
        self.actions_layout.setColumnStretch(0, 1)
        self.actions_layout.setColumnStretch(1, 1)
        self.actions_layout.setColumnStretch(2, 1)

    def refresh_responsive_layout(self) -> None:
        """Reflow from the space that remains after navigation panels."""
        width = self.width()
        height = self.height()
        mode = "wide" if width >= 1400 else "medium" if width >= 1200 else "compact"
        mode_changed = mode != self.responsive_mode
        self.responsive_mode = mode

        margins = 20 if mode == "wide" else 12 if mode == "medium" else 8
        spacing = 18 if mode == "wide" else 12 if mode == "medium" else 8
        if self.root_layout is not None:
            self.root_layout.setContentsMargins(margins, margins, margins, margins)
            self.root_layout.setSpacing(spacing)

        # At 900-1050 px the sidebar left too little working width and forced
        # horizontal scrolling. The compact toolbar preserves every action.
        sidebar_visible = mode != "compact"
        sidebar_width = 290 if mode == "wide" else 250 if mode == "medium" else 220
        if self.sidebar_scroll_area is not None:
            self.sidebar_scroll_area.setVisible(sidebar_visible)
            self.sidebar_scroll_area.setFixedWidth(sidebar_width)
        self.compact_toolbar.setVisible(not sidebar_visible)

        if self.main_splitter is not None and self.log_panel is not None:
            show_log_details = self.log_expanded_for_installation
            self.log_panel.setVisible(show_log_details)
            if self.log_title is not None:
                self.log_title.setText(
                    "Canlı İşlem Günlüğü" if show_log_details else "Canlı Günlük"
                )
            if self.log_subtitle is not None:
                self.log_subtitle.setVisible(show_log_details)
            if self.log_filter_shell is not None:
                self.log_filter_shell.setVisible(show_log_details)
            if not show_log_details:
                # Before setup starts, the operator needs the form—not an
                # empty log surface. Keep the splitter panel fully collapsed
                # and reveal it only when live progress is useful.
                self.main_splitter.setOrientation(Qt.Orientation.Horizontal)
                self.main_splitter.setSizes([max(1, width), 0])
            elif mode == "wide":
                self.main_splitter.setOrientation(Qt.Orientation.Horizontal)
                self.log_panel.setMinimumHeight(0)
                self.log_panel.setMaximumHeight(16777215)
                # A narrow log panel made both messages and filter labels wrap
                # unnecessarily on wide screens.
                self.log_panel.setMinimumWidth(300)
                self.log_panel.setMaximumWidth(420)
                available_width = max(1120, width - sidebar_width - (margins * 2) - spacing)
                log_share = 0.26
                self.main_splitter.setSizes(
                    [int(available_width * (1 - log_share)), int(available_width * log_share)]
                )
            else:
                self.main_splitter.setOrientation(Qt.Orientation.Vertical)
                self.log_panel.setMinimumWidth(0)
                self.log_panel.setMaximumWidth(16777215)
                if self.log_expanded_for_installation:
                    self.log_panel.setMinimumHeight(250)
                    self.log_panel.setMaximumHeight(360 if mode == "medium" else 300)
                    log_height = 320 if mode == "medium" else 270
                else:
                    self.log_panel.setMinimumHeight(120)
                    self.log_panel.setMaximumHeight(170 if mode == "medium" else 150)
                    log_height = 140 if mode == "medium" else 130
                self.main_splitter.setSizes([max(320, height - log_height - margins * 2), log_height])

        if mode_changed:
            self._reflow_grid(self.summary_grid, self.summary_tiles, 3 if mode == "wide" else 2 if mode == "medium" else 1)
            self._reflow_grid(self.device_summary_grid, self.device_summary_tiles, 4 if mode == "wide" else 2 if mode == "medium" else 1)
            self._refresh_profile_layout(mode)
            self._refresh_identity_layout(mode)
            self._reflow_grid(self.options_group_grid, self.option_group_widgets, 2 if mode == "wide" else 1)
            self._refresh_actions_layout(mode)
            self._refresh_report_layout(mode)
        self.refresh_log_filter_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refresh_responsive_layout()

    def _connect_signals(self) -> None:
        self.generate_button.clicked.connect(self.generate_identity)
        self.create_button.clicked.connect(self.start_onboarding)
        self.terminate_button.clicked.connect(self.start_terminate)
        self.clear_log_button.clicked.connect(self.clear_live_log)
        self.domain_leave_button.clicked.connect(self.start_domain_leave)
        self.backup_button.clicked.connect(self.start_backup)
        self.preflight_button.clicked.connect(self.start_preflight)
        self.apply_profile_button.hide()
        self.profile_combo.currentIndexChanged.connect(self.apply_selected_profile)
        self.profile_combo.currentTextChanged.connect(self.update_profile_note)
        self.profile_combo.currentTextChanged.connect(lambda: self.update_summary())
        self.user_type_combo.currentTextChanged.connect(lambda: self.update_summary())
        self.user_type_combo.currentTextChanged.connect(self.refresh_option_states)
        self.company_combo.currentTextChanged.connect(lambda: self.update_summary())
        self.company_combo.currentTextChanged.connect(self.auto_generate_identity)
        self.full_name_input.textChanged.connect(lambda: self.update_summary())
        self.full_name_input.textChanged.connect(self.auto_generate_identity)
        self.username_output.textChanged.connect(lambda: self.update_summary())
        self.report_table.itemSelectionChanged.connect(self.load_selected_report_detail)
        self.settings_button.clicked.connect(self.open_settings)
        self.compact_settings_button.clicked.connect(self.open_settings)

    def apply_config_to_widgets(self) -> None:
        brand_title = self.normalized_brand_title(self.config.branding.title)
        self.setWindowTitle(brand_title)
        self.brand_title.setText(brand_title)
        self.brand_subtitle.setText(self.config.branding.subtitle or "Yeni cihaz kurulum ve onboarding paneli")
        self.header_title.setText("Yeni Cihaz Kurulumu")
        self.header_subtitle.setText("Kullanıcı oluşturma, kurulum adımları, ön kontrol ve ikinci faz otomasyon tek ekranda yönetilir.")
        self.quick_note.setText(self.build_quick_note())
        self.admin_chip.setText("Yönetici Modu" if self.service.is_admin_session() else "Yönetici Gerekli")
        self.config_chip.setText(f"{len(self.config.profiles)} profil")
        self.destination_input.setText(self.config.backup.network_path)
        self.populate_backup_users()

        packaged_logo = self.config.base_dir / "assets" / "acik_logo.png"
        logo_path = packaged_logo if packaged_logo.is_file() else self.config.branding.logo_path
        pixmap = QPixmap(str(logo_path))
        if not pixmap.isNull():
            self.logo_label.setPixmap(
                pixmap.scaled(
                    150,
                    150,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            # A partially copied portable package must not leave the brand
            # area blank. The next valid package restores the image asset.
            self.logo_label.setText("AÇIK")
            self.logo_label.setStyleSheet("font-size: 28px; font-weight: 700; color: #b28b46;")

        current_company = self.company_combo.currentText().strip()
        self.company_combo.blockSignals(True)
        self.company_combo.clear()
        self.company_combo.addItems(list(self.config.companies.keys()))
        if current_company and current_company in self.config.companies:
            self.company_combo.setCurrentText(current_company)
        self.company_combo.blockSignals(False)

        current_user_type = self.user_type_combo.currentText().strip()
        self.user_type_combo.blockSignals(True)
        self.user_type_combo.clear()
        self.user_type_combo.addItems(self.config.user_types)
        if current_user_type and current_user_type in self.config.user_types:
            self.user_type_combo.setCurrentText(current_user_type)
        elif "Lokal" in self.config.user_types:
            self.user_type_combo.setCurrentText("Lokal")
        self.user_type_combo.blockSignals(False)

        current_profile = self.profile_combo.currentText().strip()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(list(self.config.profiles.keys()))
        if current_profile and current_profile in self.config.profiles:
            self.profile_combo.setCurrentText(current_profile)
        elif self.profile_combo.count() > 0:
            self.profile_combo.setCurrentIndex(0)
        self.profile_combo.blockSignals(False)
        self.apply_profile_button.setEnabled(self.profile_combo.count() > 0)

        profile_to_apply = ""
        if self.active_profile_name and self.active_profile_name in self.config.profiles:
            self.profile_combo.setCurrentText(self.active_profile_name)
            profile_to_apply = self.active_profile_name
        elif self.profile_combo.count() > 0:
            profile_to_apply = self.profile_combo.currentText().strip()

        if profile_to_apply:
            self.apply_profile_to_form(profile_to_apply)
        else:
            self.active_profile_name = ""
            self.update_profile_note()
            self.refresh_option_states()
            self.update_summary()
            self.reset_step_statuses_from_ui()

        self.auto_generate_identity()

        self.load_last_preflight()
        self.refresh_report_history()
        self.set_log_filter("important")
        if hasattr(self, "usb_status_label"):
            self.scan_usb_util_path()
        self.refresh_active_workflow_card()

    def build_quick_note(self) -> str:
        required_wifi = self.config.network_resources.required_wifi_ssid.strip()
        if not required_wifi:
            required_wifi = self.config.wifi_profiles.get("domain_join").ssid if self.config.wifi_profiles.get("domain_join") else ""
        network_text = required_wifi or "tanımlı değil"
        return (
            f"Wi-Fi seciliyse baglanti \"{network_text}\" agi uzerinden kurulur ve saat esitlemesi burada yapilir. "
            f"On kontrol raporu yazilir, ikinci faz gorevler yeni kullanıcı oturumunda aynı ağ üzerinde tamamlanır."
        )

    def on_option_toggled(self, option_name: str, button: QPushButton, state: bool) -> None:
        button.setText("Açık" if state else "Kapalı")
        button.setAccessibleName(f"{OPTION_LABELS.get(option_name, option_name)}: {'Açık' if state else 'Kapalı'}")
        # Administrator seçimi sabit duvar kagidi politikasini devre disi
        # birakir; bu politika yalnizca olusturulan yerel standart kullanici
        # icindir.
        if option_name in {"administrator", "desktop_wallpaper"}:
            self.refresh_option_states()
        self.update_summary()
        self.reset_step_statuses_from_ui()
        if option_name in ("hackbgrt", "eset"):
            usb_cb = self.usb_checkboxes.get(option_name)
            if usb_cb:
                usb_cb.setChecked(state)

    @staticmethod
    def normalized_brand_title(title: str) -> str:
        """Repair an old Windows-1252/UTF-8 mojibake brand title for display."""
        value = (title or "").strip() or "AÇIK Kurulum"
        return repair_display_text(value) or value

    def repair_visible_texts(self) -> None:
        repair_widget_texts(self)

    def update_summary(self) -> None:
        selected_combo_profile = self.profile_combo.currentText().strip()
        if self.active_profile_name:
            profile_text = self.active_profile_name
            if selected_combo_profile and selected_combo_profile != self.active_profile_name:
                profile_text = f"{self.active_profile_name} | listede {selected_combo_profile}"
        elif selected_combo_profile:
            profile_text = f"{selected_combo_profile} | hazır"
        else:
            profile_text = "Özel Seçim"
        self.summary_profile_value.setText(profile_text)
        self.summary_company_value.setText(self.company_combo.currentText().strip() or "Seçilmedi")
        self.summary_user_type_value.setText(self.user_type_combo.currentText().strip() or "Seçilmedi")
        self.summary_username_value.setText(self.username_output.text().strip() or "Henüz üretilmedi")
        self.summary_pc_value.setText(self.pc_name_output.text().strip() or "Henüz üretilmedi")
        
        # Ağ Yazıcısı kontrolü: Yalnızca Domain içi seçilebilir olsun
        is_domain = self.user_type_combo.currentText().strip() == "Domain"
        username = self.username_output.text().strip() or "<kullanici adi>"
        domain_short_name = self.config.domain.name.split(".", 1)[0].strip() or "DOMAIN"
        if is_domain:
            self.domain_signin_hint.setText(
                "Domain Windows girisi: "
                f"{domain_short_name}\\{username}. Yeniden baslatmadan sonra Kullanici adi alanina bu bicimi yazin."
            )
            self.domain_signin_hint.show()
        else:
            self.domain_signin_hint.hide()
        if "network_printer" in self.checkboxes:
            printer_toggle = self.checkboxes["network_printer"]
            printer_toggle.setEnabled(is_domain)
            if not is_domain and printer_toggle.isChecked():
                # The toggled signal refreshes the summary once. Calling the
                # handler directly here would recursively call update_summary.
                printer_toggle.setChecked(False)
        enabled_count = sum(1 for button in self.checkboxes.values() if button.isChecked())
        deferred_count = sum(
            1
            for name in ("main_file_server", "network_printer", "desktop_wallpaper", "desktop_signature", "classic_outlook")
            if name in self.checkboxes and self.checkboxes[name].isChecked()
        )
        summary_text = f"{enabled_count} adım açık"
        if deferred_count:
            summary_text += f" | {deferred_count} ikinci faz"
        self.summary_steps_value.setText(summary_text)
        self.refresh_preflight_staleness()

    def current_request_for_fingerprint(self) -> OnboardingRequest:
        """Build an unvalidated snapshot used only to detect stale preflight data."""
        return OnboardingRequest(
            profile_name=self.active_profile_name,
            full_name=self.full_name_input.text().strip(),
            company_name=self.company_combo.currentText().strip(),
            user_type=self.user_type_combo.currentText().strip(),
            username=self.username_output.text().strip(),
            computer_name=self.pc_name_output.text().strip(),
            password="",
            options={name: button.isChecked() for name, button in self.checkboxes.items()},
        )

    def refresh_preflight_staleness(self, payload: dict[str, object] | None = None) -> None:
        payload = payload if payload is not None else self.service.load_last_preflight()
        if not payload:
            return
        current_fingerprint = self.service.request_fingerprint(self.current_request_for_fingerprint())
        if payload.get("request_fingerprint") == current_fingerprint:
            return
        self.preflight_status_label.setText(
            "Form ön kontrolden sonra değişti. Kurulumu başlatmadan önce sistemi yeniden kontrol edin."
        )
        self.compact_preflight_status.setText("Ön Kontrol: Yenile")

    def build_expected_steps(self, request: OnboardingRequest) -> list[str]:
        options = request.options
        steps: list[str] = []
        if options.get("rename_admin"):
            steps.append("Lokaladm")
        if request.user_type == "Lokal":
            steps.append("Yerel kullanici")
            if options.get("ip_admin"):
                steps.append("IP Admin")
            if options.get("administrator"):
                steps.append("Administrator")
            steps.append("Bilgisayar adi")
        elif request.user_type == "Domain":
            steps.append("Domain katilimi")
            if options.get("ip_admin"):
                steps.append("IP Admin")
            if options.get("administrator"):
                steps.append("Administrator")
        if options.get("anydesk"):
            steps.append("AnyDesk")
        if options.get("wifi_sync"):
            steps.append("Wi-Fi ve saat")
        if options.get("windows_activation"):
            steps.append("Windows etkinlestirme")
        if options.get("windows_update"):
            steps.append("Windows Update")
        if options.get("eset"):
            steps.append("ESET")
        if options.get("hackbgrt"):
            steps.append("HackBGRT")
        local_standard_wallpaper = bool(
            request.user_type == "Lokal"
            and options.get("desktop_wallpaper")
            and not options.get("administrator")
        )
        has_deferred_work = (
            options.get("main_file_server")
            or options.get("network_printer")
            or local_standard_wallpaper
            or options.get("desktop_signature")
            or options.get("classic_outlook")
            or options.get("eset")
            or options.get("windows_update")
            or (request.user_type == "Domain" and (options.get("ip_admin") or options.get("administrator")))
        )
        if has_deferred_work:
            steps.append("Ikinci faz gorevleri")
        if options.get("delete_x_user"):
            steps.append("X otomatik giris kapatma")
            steps.append("x kullanicisi temizligi")
        if request.user_type == "Domain" or options.get("restart") or has_deferred_work or options.get("delete_x_user"):
            steps.append("Yeniden baslat")
        return steps

    def reset_step_statuses(self, steps: list[str]) -> None:
        self.step_table.setRowCount(0)
        self.current_running_step = ""
        for row, step_name in enumerate(steps):
            self.step_table.insertRow(row)
            self.step_table.setItem(row, 0, QTableWidgetItem(step_name))
            self.step_table.setItem(row, 1, QTableWidgetItem("Bekliyor"))
            self.step_table.setItem(row, 2, QTableWidgetItem("Sırasını bekliyor."))
            self.step_table.setRowHeight(row, 30)
        if steps:
            self.step_hint_label.setText("Kurulum başladığında her adım burada sırayla güncellenir.")
            self.compact_step_status.setText(f"Adım: 0/{len(steps)}")
        else:
            self.step_hint_label.setText("Kurulum başlamadan önce açık adımlara göre akış listesi hazırlanır.")
            self.compact_step_status.setText("Adım: Bekliyor")

    def reset_step_statuses_from_ui(self) -> None:
        request = OnboardingRequest(
            profile_name=self.active_profile_name,
            full_name=self.full_name_input.text().strip(),
            company_name=self.company_combo.currentText().strip(),
            user_type=self.user_type_combo.currentText().strip(),
            username=self.username_output.text().strip(),
            computer_name=self.pc_name_output.text().strip(),
            password=self.password_output.text().strip(),
            options={name: checkbox.isChecked() for name, checkbox in self.checkboxes.items()},
        )
        self.reset_step_statuses(self.build_expected_steps(request))

    def set_step_status(self, step_name: str, status: str, detail: str) -> None:
        for row in range(self.step_table.rowCount()):
            item = self.step_table.item(row, 0)
            if item and item.text() == step_name:
                self.step_table.item(row, 1).setText(status)
                self.step_table.item(row, 2).setText(detail)
                completed = sum(
                    1
                    for index in range(self.step_table.rowCount())
                    if self.step_table.item(index, 1)
                    and self.step_table.item(index, 1).text()
                    in {"Tamamlandı", "Başarılı", "OK", "Hata"}
                )
                self.compact_step_status.setText(
                    f"Adım: {completed}/{self.step_table.rowCount()} | {status}"
                )
                return
        row = self.step_table.rowCount()
        self.step_table.insertRow(row)
        self.step_table.setItem(row, 0, QTableWidgetItem(step_name))
        self.step_table.setItem(row, 1, QTableWidgetItem(status))
        self.step_table.setItem(row, 2, QTableWidgetItem(detail))
        self.step_table.setRowHeight(row, 30)

    def infer_log_level(self, message: str) -> str:
        lowered = message.lower()
        if "hata" in lowered or "basarisiz" in lowered or "kritik" in lowered or "bulunamadı" in lowered or "bulunamadi" in lowered or "bulunmadı" in lowered or "bulunmadi" in lowered:
            return "error"
        if "uyarı" in lowered or "uyari" in lowered or "warning" in lowered:
            return "warning"
        if "tamamlandı" in lowered or "tamamlandi" in lowered or "hazırlandı" in lowered or "hazirlandi" in lowered or "başarı" in lowered or "basari" in lowered:
            return "success"
        return "info"

    def set_log_filter(self, filter_name: str) -> None:
        self.current_log_filter = filter_name
        for key, button in self.log_filter_buttons.items():
            button.setChecked(key == filter_name)
        self.refresh_log_view()

    def refresh_log_view(self) -> None:
        filtered_rows: list[str] = []
        for entry in self.log_entries:
            if self.current_log_filter == "important":
                if entry["level"] == "info":
                    continue
            elif self.current_log_filter != "all" and entry["level"] != self.current_log_filter:
                continue
            filtered_rows.append(
                f'[{entry["time"]}] [{entry["label"]}] {entry["text"]}'
            )

        # Düz metin render'ı canlı log için daha hızlı ve ekran sürücüsünden
        # bağımsızdır. Seviye filtresi ve [HATA]/[UYARI] etiketleri korunur.
        self.log_output.setPlainText("\n".join(filtered_rows))
        cursor = self.log_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_output.setTextCursor(cursor)

    def clear_live_log(self) -> None:
        self.log_entries.clear()
        self.log_output.clear()

    def update_step_progress_from_log(self, message: str) -> None:
        if message.startswith("Adim basladi: "):
            step_name = message.removeprefix("Adim basladi: ").strip()
            self.current_running_step = step_name
            self.set_step_status(step_name, "Çalışıyor", "Adım şu anda yürütülüyor.")
            return
        if message.startswith("Adim tamamlandi: "):
            step_name = message.removeprefix("Adim tamamlandi: ").strip()
            self.current_running_step = ""
            self.set_step_status(step_name, "Tamamlandı", "Adım başarıyla bitti.")
            return
        if message.startswith("Adim atlandi: "):
            content = message.removeprefix("Adim atlandi: ").strip()
            if " - " in content:
                step_name, detail = content.split(" - ", 1)
            else:
                step_name, detail = content, "Adım atlandı."
            self.current_running_step = ""
            self.set_step_status(step_name.strip(), "Atlandı", detail.strip())
            return
        if message.startswith("Adim basarisiz: "):
            content = message.removeprefix("Adim basarisiz: ").strip()
            step_name, _, detail = content.partition(" - ")
            self.current_running_step = ""
            self.set_step_status(step_name.strip(), "Hata", detail.strip() or "Adim tamamlanamadi.")
            return
        if "IP Admin ve Administrator secenekleri domain akisinda atlandi." in message:
            self.set_step_status("Domain yetkileri", "Atlandı", "Domain akışında bu adımlar çalıştırılmıyor.")

    def load_last_preflight(self) -> None:
        payload = self.service.load_last_preflight()
        self.preflight_table.setRowCount(0)
        if not payload:
            self.preflight_status_label.setText("Ön kontrol henüz çalıştırılmadı.")
            self.compact_preflight_status.setText("Ön Kontrol: Bekliyor")
            return

        checks = payload.get("checks", [])
        errors = 0
        warnings = 0
        for row, check in enumerate(checks if isinstance(checks, list) else []):
            if not isinstance(check, dict):
                continue
            status = str(check.get("status", "")).strip()
            if status == "error":
                errors += 1
            elif status == "warning":
                warnings += 1
            self.preflight_table.insertRow(row)
            self.preflight_table.setItem(row, 0, QTableWidgetItem(str(check.get("name", ""))))
            self.preflight_table.setItem(row, 1, QTableWidgetItem(status.upper() or "-"))
            self.preflight_table.setItem(row, 2, QTableWidgetItem(str(check.get("detail", ""))))
            self.preflight_table.setRowHeight(row, 30)

        if errors:
            self.preflight_status_label.setText(f"Son ön kontrolde {errors} kritik hata bulundu.")
            self.compact_preflight_status.setText(f"Ön Kontrol: {errors} Hata")
        elif warnings:
            self.preflight_status_label.setText(f"Son ön kontrolde {warnings} uyarı bulundu.")
            self.compact_preflight_status.setText(f"Ön Kontrol: {warnings} Uyarı")
        else:
            self.preflight_status_label.setText("Son ön kontrol temiz görünüyor. Kurulum hazır.")
            self.compact_preflight_status.setText("Ön Kontrol: Hazır")
        self.refresh_preflight_staleness(payload)

    def refresh_report_history(self) -> None:
        reports = self.service.load_report_history()
        self.report_table.setRowCount(0)
        self.report_detail.clear()
        if not reports:
            self.report_summary_label.setText("Henüz rapor bulunmuyor.")
            return

        completed = sum(1 for report in reports if report.get("status") == "completed")
        failed = sum(1 for report in reports if report.get("status") == "failed")
        partial = sum(1 for report in reports if report.get("status") == "partial")
        pending = sum(
            1
            for report in reports
            if report.get("status") in {"awaiting_post_login", "pending", "running"}
        )
        self.report_summary_label.setText(
            f"{len(reports)} rapor | {completed} başarılı | {partial} kısmi | "
            f"{pending} bekleyen | {failed} hatalı"
        )

        for row, report in enumerate(reports):
            self.report_table.insertRow(row)
            self.report_table.setItem(row, 0, QTableWidgetItem(str(report.get("run_started_at", report.get("run_at", "")))))
            self.report_table.setItem(row, 1, QTableWidgetItem(str(report.get("status", ""))))
            self.report_table.setItem(row, 2, QTableWidgetItem(str(report.get("company_name", ""))))
            self.report_table.setItem(row, 3, QTableWidgetItem(str(report.get("username", ""))))
            self.report_table.setItem(row, 4, QTableWidgetItem(str(report.get("computer_name", ""))))
            self.report_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, report)
            self.report_table.setRowHeight(row, 38)
            
    def delete_all_reports(self) -> None:
        confirmed = self.ask_confirmation("Onay", "Tüm raporlar kalıcı olarak silinecek. Emin misiniz?")
        if confirmed:
            failed = self.service.delete_all_reports()
            self.refresh_report_history()
            if failed:
                self.show_message(
                    "Raporlar Kısmen Silindi",
                    "Bazı rapor dosyaları silinemedi (kilitli/izin reddedildi olabilir): "
                    + ", ".join(failed),
                    "warning",
                )
            else:
                self.show_message("Raporlar Silindi", "Tüm geçmiş raporlar başarıyla silindi.", "success")

        if self.report_table.rowCount() > 0:
            self.report_table.selectRow(0)
            self.load_selected_report_detail()

    def load_selected_report_detail(self) -> None:
        row = self.report_table.currentRow()
        if row < 0 or self.report_table.item(row, 0) is None:
            return
        report = self.report_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if not isinstance(report, dict):
            return

        status = html_escape(str(report.get('status', '')).upper())
        status_colors = {
            "COMPLETED": "#2e7d32",
            "OK": "#2e7d32",
            "PARTIAL": "#b26a00",
            "AWAITING_POST_LOGIN": "#8f7238",
            "PENDING": "#8f7238",
            "RUNNING": "#8f7238",
            "CLOSED": "#6b6258",
        }
        status_color = status_colors.get(status, "#c62828")
        
        # En onemli olayları özetle
        steps = report.get("steps", [])
        highlights = []
        detailed_steps_html = []
        
        # Kritik adımları tespit et
        user_created = False
        user_failed = False
        user_fail_detail = ""

        pc_renamed = False
        pc_failed = False
        pc_fail_detail = ""

        x_deleted = False
        x_cleanup_scheduled = False
        x_failed = False
        x_fail_detail = ""
        x_cleanup_status = ""

        eset_installed = False
        eset_failed = False
        eset_fail_detail = ""

        wifi_synced = False
        lock_screen_ready = False
        lock_screen_failed = False
        lock_screen_fail_detail = ""

        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                name = step.get('name', '')
                st = step.get('status', '')
                detail = step.get('detail', '')

                # Detay listesi icin HTML hazirla
                if st == "ok":
                    st_html = '<span class="step-ok">✔ Başarılı</span>'
                elif st == "error" or st == "failed":
                    st_html = '<span class="step-failed">❌ Hata</span>'
                else:
                    st_html = '<span class="step-skipped">⚠ Atlandı</span>'
                detailed_steps_html.append(
                    f"<li><b>{html_escape(str(name))}</b>: {st_html} | "
                    f"<i>{html_escape(str(detail))}</i></li>"
                )

                # Durum bayraklarini guncelle
                if name == "Yerel kullanici":
                    if st == "ok":
                        user_created = True
                    else:
                        user_failed = True
                        user_fail_detail = detail
                elif name == "Bilgisayar adi":
                    if st == "ok":
                        pc_renamed = True
                    else:
                        pc_failed = True
                        pc_fail_detail = detail
                elif name == "x kullanicisi temizligi":
                    # The initial report can only say that a cleanup plan was
                    # registered.  The account is deleted by SYSTEM later,
                    # after the target user's logon.  Never infer deletion
                    # from an initial step label or its successful status.
                    if st in {"error", "failed"}:
                        x_failed = True
                        x_fail_detail = detail
                elif name == "Wi-Fi ve saat" and st == "ok":
                    wifi_synced = True
                elif name == "ESET":
                    if st == "ok":
                        eset_installed = True
                    elif st != "skipped" and st != "ignored":
                        eset_failed = True
                        eset_fail_detail = detail

        post_login = report.get("post_login", {})
        if isinstance(post_login, dict):
            post_login_tasks = post_login.get("tasks", {})
            if isinstance(post_login_tasks, dict):
                delete_task = post_login_tasks.get("delete_x_user", {})
                if isinstance(delete_task, dict) and bool(delete_task.get("enabled")):
                    x_cleanup_status = str(delete_task.get("status", "")).casefold()
                    if x_cleanup_status == "succeeded":
                        x_deleted = True
                    elif x_cleanup_status in {"pending", "running", "retryable_failed"}:
                        x_cleanup_scheduled = True
                    elif x_cleanup_status in {"permanent_failed", "failed"}:
                        x_failed = True
                        x_fail_detail = str(delete_task.get("error", ""))

                # Kilit ekrani ilkesi de x temizligi gibi SYSTEM finalizasyonunda,
                # hedef oturum acildiktan sonra uygulanir; bu yuzden ilk raporun
                # "steps" listesinde degil, post_login.tasks'ta durumu bulunur.
                lock_screen_task = post_login_tasks.get("lock_screen", {})
                if isinstance(lock_screen_task, dict) and bool(lock_screen_task.get("enabled")):
                    lock_screen_status = str(lock_screen_task.get("status", "")).casefold()
                    if lock_screen_status == "succeeded":
                        lock_screen_ready = True
                    elif lock_screen_status in {"permanent_failed", "failed"}:
                        lock_screen_failed = True
                        lock_screen_fail_detail = str(lock_screen_task.get("error", ""))

        selected_options = report.get("selected_options", {})
        if (
            isinstance(selected_options, dict)
            and bool(selected_options.get("delete_x_user"))
            and not x_deleted
            and not x_failed
        ):
            # Before post-login state is written, the only honest status is
            # pending: X has not yet been removed.
            x_cleanup_scheduled = True

        # Ozeti renkli sekilde olustur
        if user_created:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>✅ Yeni Kullanıcı:</b> <span style='color: #27ae60;'><b>{html_escape(str(report.get('username', '')))}</b></span> hesabı başarıyla oluşturuldu.</div>")
        elif user_failed:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>❌ Yeni Kullanıcı:</b> <span style='color: #c0392b;'><b>Oluşturulamadı!</b> ({html_escape(str(user_fail_detail))})</span></div>")

        if pc_renamed:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>💻 Bilgisayar Adı:</b> Hedef ad <span style='color: #2980b9;'><b>{html_escape(str(report.get('computer_name', '')))}</b></span> Windows kayıtlarında doğrulandı; yeniden başlatmadan sonra etkinleşecek.</div>")
        elif pc_failed:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>❌ Bilgisayar Adı:</b> <span style='color: #c0392b;'>Güncellenemedi! ({html_escape(str(pc_fail_detail))})</span></div>")

        if lock_screen_ready:
            highlights.append("<div style='margin-bottom: 8px;'><b>Kilit Ekrani:</b> yeniden baslatmadan once makine ilkesi dogrulandi.</div>")
        elif lock_screen_failed:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>Kilit Ekrani:</b> <span style='color: #d35400;'>Uygulanamadi. ({html_escape(str(lock_screen_fail_detail))})</span></div>")

        if x_deleted:
            highlights.append("<div style='margin-bottom: 8px;'><b>🧹 Eski Kullanıcı Temizliği:</b> <span style='color: #d35400;'><b>x kullanıcısı ve profili sistemden temizlendi.</b></span></div>")
        elif x_cleanup_scheduled:
            highlights.append(
                "<div style='margin-bottom: 8px;'><b>Eski Kullanıcı Temizliği:</b> "
                "<span style='color: #8f7238;'><b>X henüz silinmedi.</b> "
                "SYSTEM temizliği ve doğrulaması bekleniyor.</span></div>"
            )
        elif x_failed:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>⚠ Eski Kullanıcı Temizliği:</b> <span style='color: #7f8c8d;'>x kullanıcısı silinemedi veya atlandı. ({html_escape(str(x_fail_detail))})</span></div>")

        if wifi_synced:
            highlights.append("<div style='margin-bottom: 8px;'><b>🌐 Wi-Fi & Saat:</b> Kurumsal Wi-Fi profili başarıyla kuruldu ve saat eşitlendi.</div>")

        if eset_installed:
            highlights.append("<div style='margin-bottom: 8px;'><b>🛡️ ESET Kurulumu:</b> ESET Antivirüs istemcisi başarıyla kuruldu.</div>")
        elif eset_failed:
            highlights.append(f"<div style='margin-bottom: 8px;'><b>⚠ ESET Kurulumu:</b> <span style='color: #d35400;'>ESET kurulamadı (Atlandı). Hata: {html_escape(str(eset_fail_detail))}</span></div>")

        if not highlights:
            highlights.append("<div>Kritik özet bilgi bulunmuyor.</div>")

        html = f"""
        <html>
        <head>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 13px; color: #30261d; line-height: 1.5; }}
            h3 {{ color: #473728; margin-top: 15px; margin-bottom: 8px; border-bottom: 1px solid #e2d0aa; padding-bottom: 3px; font-size: 14px; }}
            .highlight-box {{ background-color: #f6efe2; border: 1px solid #dcd1b4; border-left: 5px solid #d35400; padding: 12px; margin-bottom: 15px; border-radius: 6px; color: #30261d; }}
            .status-badge {{ display: inline-block; padding: 3px 8px; border-radius: 3px; font-weight: bold; color: #ffffff; }}
            .step-ok {{ color: #27ae60; font-weight: bold; }}
            .step-failed {{ color: #c0392b; font-weight: bold; }}
            .step-skipped {{ color: #7f8c8d; font-weight: bold; }}
            ul {{ margin-top: 5px; margin-bottom: 10px; padding-left: 20px; }}
        </style>
        </head>
        <body>
            <h3>Kurulum Özeti</h3>
            <div style="margin-bottom: 12px;">
                <b>Genel Durum:</b> <span class="status-badge" style="background-color: {status_color};">{status}</span>
            </div>
            
            <div class='highlight-box'>
                <b>Önemli Gelişmeler (Kritik Sonuçlar):</b>
                <div style="margin-top: 8px;">
                    {"".join(highlights)}
                </div>
            </div>
            
            <div style="background-color: #fbf7ef; border: 1px solid #efe3ca; padding: 10px; border-radius: 6px; margin-bottom: 15px; color: #473728;">
                <b>Profil:</b> {html_escape(str(report.get('profile_name', '')))}<br/>
                <b>Şirket:</b> {html_escape(str(report.get('company_name', '')))}<br/>
                <b>Başlangıç:</b> {html_escape(str(report.get('run_started_at', report.get('run_at', ''))))}<br/>
                <b>Bitiş:</b> {html_escape(str(report.get('run_finished_at', '')))}<br/>
                <b>Cihaz:</b> {html_escape(str(report.get('computer_name', '')))}
            </div>

            <h3>Diğer Gelişmeler (Tüm Teknik Detaylar)</h3>
            <ul>
                {"".join(detailed_steps_html)}
            </ul>
        """
        
        error_text = str(report.get("error", "")).strip()
        if error_text:
            html += f"""
            <h3 style='color: #c0392b; margin-top: 15px;'>Hata Ayrıntısı</h3>
            <div style='background-color: #fde8e8; border: 1px solid #f8b4b4; border-left: 5px solid #e74c3c; padding: 10px; border-radius: 6px; color: #9b1c1c;'>
                {html_escape(error_text)}
            </div>
            """
            
        html += """
        </body>
        </html>
        """
        self.report_detail.setHtml(html)

    def update_profile_note(self) -> None:
        profile_name = self.profile_combo.currentText().strip()
        profile = self.service.resolve_profile(profile_name)
        if profile is None:
            self.profile_note.setText("Hazır bir profil seçersen kullanıcı tipi, şirket ve kurulum adımları tek tıkla dolar.")
            return

        meta_parts: list[str] = []
        if profile.user_type:
            meta_parts.append(f"Kullanıcı tipi: {profile.user_type}")
        if profile.company_name:
            meta_parts.append(f"Şirket: {profile.company_name}")
        enabled_count = sum(1 for value in self.service.build_profile_options(profile_name).values() if value)
        meta_parts.append(f"{enabled_count} adım açık")
        note = profile.note.strip() or "Seçilen profil için standart kurulum ayarları hazır."
        self.profile_note.setText(f"{note}  {' | '.join(meta_parts)}. Ekrana almak için Profili Uygula düğmesini kullanabilirsin.")

    def set_option_states(self, options: dict[str, bool]) -> None:
        for name, button in self.checkboxes.items():
            checked = bool(options.get(name, False))
            button.blockSignals(True)
            button.setChecked(checked)
            button.setText("Açık" if checked else "Kapalı")
            button.blockSignals(False)

            # Profil uygulandığında da USB sekmesini otomatik eşitle
            if name in ("hackbgrt", "eset"):
                usb_cb = self.usb_checkboxes.get(name)
                if usb_cb:
                    usb_cb.setChecked(checked)
        self.refresh_option_states()

    def apply_profile_to_form(self, profile_name: str, notify: bool = False) -> bool:
        profile = self.service.resolve_profile(profile_name)
        if profile is None:
            return False

        if profile.user_type and profile.user_type in self.config.user_types:
            self.user_type_combo.setCurrentText(profile.user_type)
        if profile.company_name and profile.company_name in self.config.companies:
            self.company_combo.setCurrentText(profile.company_name)

        self.set_option_states(self.service.build_profile_options(profile_name))
        self.active_profile_name = profile_name
        self.update_profile_note()
        self.update_summary()
        self.reset_step_statuses_from_ui()

        if self.full_name_input.text().strip():
            try:
                self.generate_identity()
            except Exception:
                pass

        self.append_log(f"Profil uygulandı: {profile_name}")
        if notify:
            self.show_message("Profil", f"{profile_name} profili ekrana uygulandı.", "info")
        return True

    def apply_selected_profile(self) -> None:
        profile_name = self.profile_combo.currentText().strip()
        if not self.apply_profile_to_form(profile_name, notify=True):
            self.show_message("Profil", "Uygulanacak bir profil seçilmedi.", "warning")
            return

    def refresh_option_states(self) -> None:
        availability = {
            "hackbgrt": "HackBGRT yolu ayarlarda boş.",
            "main_file_server": "File Server ayarları eksik.",
            "network_printer": "Yazıcı ayarları eksik.",
            "desktop_wallpaper": "Sabit arka plan kaynağı veya hedefi eksik.",
            "desktop_signature": "İmza kaynağı ayarlarda eksik.",
            "classic_outlook": "Outlook Classic ayarları eksik.",
        }
        for option_name, tooltip in availability.items():
            if option_name not in self.checkboxes:
                continue
            button = self.checkboxes[option_name]
            enabled = self.service.has_custom_action(option_name)
            button.setEnabled(enabled)
            button.setToolTip("" if enabled else tooltip)
            if not enabled and button.isChecked():
                button.setChecked(False)
                button.setText("Kapalı")

        wallpaper_button = self.checkboxes.get("desktop_wallpaper")
        if wallpaper_button is not None:
            is_local_standard = bool(
                self.user_type_combo.currentText().strip() == "Lokal"
                and not self.checkboxes.get("administrator", wallpaper_button).isChecked()
            )
            if not is_local_standard:
                wallpaper_button.setEnabled(False)
                wallpaper_button.setToolTip(
                    "Sabit arka plan yalnızca oluşturulan yerel standart kullanıcı için uygulanır."
                )
                if wallpaper_button.isChecked():
                    wallpaper_button.setChecked(False)
                    wallpaper_button.setText("Kapalı")

    def build_stylesheet(self) -> str:
        return """
        QMainWindow {
            background: #efe7da;
        }
        QWidget {
            color: #1f1b18;
            font-family: 'Segoe UI';
            font-size: 14px;
            background: transparent;
        }
        QLabel {
            background: transparent;
        }
        #appRoot {
            background: #efe7da;
        }
        #sidebar {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #211d1a, stop:1 #2c2724);
            border-radius: 30px;
        }
        #brandCard {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #fffdf8, stop:1 #f4ead4);
            border: 1px solid rgba(220, 201, 160, 0.85);
            border-radius: 24px;
        }
        #logoShell {
            background: rgba(255, 255, 255, 0.74);
            border: 1px solid rgba(220, 201, 160, 0.58);
            border-radius: 18px;
        }
        #sideCard {
            background: rgba(255, 248, 236, 0.08);
            border: 1px solid rgba(223, 209, 178, 0.14);
            border-radius: 22px;
        }
        QGroupBox#sidebarInfoCard {
            background: rgba(255, 248, 236, 0.1);
            border: 1px solid rgba(247, 233, 201, 0.16);
            border-radius: 22px;
            margin-top: 14px;
            padding: 16px;
            color: #fff8eb;
            font-weight: 800;
        }
        QGroupBox#sidebarInfoCard::title {
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 8px;
            color: #d0af68;
            font-size: 13px;
            font-weight: 800;
        }
        #brandTitle {
            color: #3a3128;
            font-size: 20px;
            font-weight: 800;
        }
        #brandSubtitle {
            color: #76624a;
            font-size: 11px;
        }
        #miniTitle {
            color: #d0af68;
            font-size: 17px;
            font-weight: 800;
        }
        #sideText {
            color: #f4eee1;
            font-size: 13px;
            line-height: 1.4;
        }
        #sidebarSteps {
            background: rgba(255, 248, 236, 0.04);
            border: 1px solid rgba(223, 209, 178, 0.12);
            border-radius: 18px;
            padding: 12px;
        }
        #sidebarStepRow {
            background: transparent;
        }
        #sidebarStepNumber {
            background: rgba(208, 175, 104, 0.18);
            border: 1px solid rgba(208, 175, 104, 0.42);
            border-radius: 10px;
            color: #f4dfab;
            font-size: 12px;
            font-weight: 800;
        }
        #sidebarStepText {
            color: #fff8ea;
            font-size: 13px;
            font-weight: 700;
        }
        #sidebarDivider {
            min-height: 1px;
            max-height: 1px;
            background: rgba(223, 209, 178, 0.16);
            border: 0;
        }
        #statusChip, #statusChipMuted {
            border-radius: 999px;
            padding: 7px 12px;
            font-size: 12px;
            font-weight: 700;
        }
        #statusChip {
            background: #d8b768;
            color: #241e18;
        }
        #statusChipMuted {
            background: rgba(255, 249, 236, 0.12);
            color: #f3e8cf;
        }
        #heroCard {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #fffaf1, stop:1 #f1e4cb);
            border: 1px solid #dfcca4;
            border-radius: 24px;
        }
        #heroTitle {
            color: #2b241e;
            font-size: 34px;
            font-weight: 800;
        }
        #heroSubtitle {
            color: #635446;
            font-size: 15px;
        }
        #heroNote {
            margin-top: 4px;
            background: rgba(215, 184, 104, 0.14);
            border: 1px solid rgba(186, 153, 82, 0.34);
            border-radius: 16px;
            padding: 12px 14px;
            color: #59452a;
            font-weight: 600;
        }
        #workflowRecoveryCard {
            background: #fff4df;
            border: 2px solid #d4ad5b;
            border-radius: 18px;
        }
        #workflowRecoveryTitle {
            color: #6d4812;
            font-size: 16px;
            font-weight: 900;
        }
        #workflowRecoveryDetail {
            color: #5e503d;
            font-size: 13px;
            font-weight: 600;
        }
        #profileNote {
            background: rgba(208, 175, 104, 0.08);
            border: 1px solid rgba(198, 167, 99, 0.26);
            border-radius: 16px;
            padding: 10px 14px;
            color: #5d4a33;
            font-size: 13px;
            font-weight: 600;
        }
        #activityFrame {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #25201d, stop:1 #3a3027);
            border: 1px solid #d0af68;
            border-radius: 18px;
        }
        #activityTitle {
            color: #fff5df;
            font-size: 15px;
            font-weight: 900;
        }
        #activityDetail {
            color: #f3e7d2;
            font-size: 12px;
        }
        #activityElapsed {
            color: #e0bd70;
            font-family: 'Cascadia Mono';
            font-size: 13px;
            font-weight: 800;
        }
        QProgressBar#activityProgress {
            min-height: 8px;
            max-height: 8px;
            border: 0;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.12);
        }
        QProgressBar#activityProgress::chunk {
            border-radius: 4px;
            background: #d8b768;
        }
        #summaryTile {
            background: #faf4e8;
            border: 1px solid #e2d2ae;
            border-radius: 18px;
            min-height: 74px;
        }
        #deviceSummaryShell {
            background: #fffaf0;
            border: 1px solid #e2d2ae;
            border-radius: 20px;
        }
        #sectionTitle {
            color: #2f2419;
            font-size: 21px;
            font-weight: 900;
        }
        #sectionSubtitle {
            color: #75624b;
            font-size: 12px;
            font-weight: 600;
        }
        #summaryTitle {
            color: #75624b;
            font-size: 12px;
            font-weight: 700;
        }
        #summaryValue {
            color: #2f2419;
            font-size: 15px;
            font-weight: 800;
        }
        QToolButton#copyIconButton {
            min-width: 28px;
            max-width: 28px;
            min-height: 28px;
            max-height: 28px;
            border: 1px solid #d5bd88;
            border-radius: 9px;
            background: #fffaf0;
            color: #6f552b;
            font-size: 18px;
            font-weight: 800;
        }
        QToolButton#copyIconButton:hover {
            background: #ead39f;
            border-color: #b58e3f;
            color: #2f2419;
        }
        #mainArea, #tabSurface {
            background: transparent;
        }
        QGroupBox {
            background: #fffdfa;
            border: 2px solid #e0cfab;
            border-radius: 20px;
            margin-top: 18px;
            padding: 20px;
            color: #3f3124;
            font-weight: 700;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 16px;
            padding: 3px 12px;
            color: #8f7238;
            background-color: #fffcf7;
            border: 2px solid #e0cfab;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 800;
        }
        #fieldBlock {
            background: transparent;
        }
        #fieldLabel {
            color: #6a5947;
            font-size: 13px;
            font-weight: 700;
        }
        #companyPasswordStatus {
            color: #6d5a43;
            font-size: 12px;
            font-weight: 600;
            padding-left: 2px;
        }
        #sidebarSectionNote {
            color: #f3ece1;
            font-size: 12px;
            font-weight: 600;
        }
        QLineEdit, QComboBox {
            min-height: 44px;
            background: #fffefb;
            border: 1px solid #d4c19a;
            border-radius: 16px;
            padding: 0 14px;
            color: #1f1b18;
            font-size: 15px;
            selection-background-color: #c8a85d;
        }
        QComboBox {
            padding-right: 52px;
        }
        QComboBox:hover {
            border: 1px solid #c29d53;
            background: #fffdfa;
        }
        QComboBox QLineEdit {
            border: 0;
            background: transparent;
            padding: 0;
            margin: 0;
            color: #1f1b18;
        }
        QLineEdit:focus, QComboBox:focus {
            border: 1px solid #b58e3f;
            background: #ffffff;
        }
        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 42px;
            border-left: 1px solid #d8c49d;
            background: #f3e4c4;
            border-top-right-radius: 16px;
            border-bottom-right-radius: 16px;
        }
        QComboBox::down-arrow {
            width: 0;
            height: 0;
            border-left: 6px solid transparent;
            border-right: 6px solid transparent;
            border-top: 7px solid #4c3928;
        }
        QComboBox QAbstractItemView {
            background: #fffdf9;
            color: #1f1b18;
            selection-background-color: #efdfba;
            selection-color: #1f1b18;
            border: 1px solid #d4c19a;
            outline: 0;
            padding: 6px;
        }
        QLineEdit[readOnly="true"] {
            background: #f8f1e1;
            color: #3d3329;
        }
        #optionGroup {
            background: #faf5eb;
            border: 1px solid #e6d7b6;
            border-radius: 20px;
        }
        #optionTitle {
            color: #423021;
            font-size: 16px;
            font-weight: 800;
        }
        #optionSubtitle {
            color: #7a6853;
            font-size: 13px;
        }
        QPushButton#sectionToggle {
            text-align: left;
            background: #f3ead7;
            color: #34281d;
            border: 1px solid #decda9;
            border-radius: 16px;
            padding: 12px 14px;
            font-size: 15px;
            font-weight: 800;
        }
        QPushButton#sectionToggle:hover {
            background: #efe1c2;
        }
        #actionRow {
            background: #fffdfa;
            border: 1px solid #e5d4b1;
            border-radius: 18px;
        }
        #actionTitle {
            color: #2e241c;
            font-size: 14px;
            font-weight: 800;
        }
        #actionDescription {
            color: #746452;
            font-size: 12px;
        }
        QPushButton#actionToggle {
            min-height: 34px;
            padding: 0 16px;
            border-radius: 14px;
            background: #5f564e;
            color: #f8f1e2;
            font-size: 13px;
            font-weight: 800;
        }
        QPushButton#actionToggle:hover {
            background: #74685f;
        }
        QPushButton#actionToggle:checked {
            background: #d0af68;
            color: #211c17;
        }
        #sectionNote {
            color: #6a5a48;
            font-size: 13px;
        }
        QTableWidget#sidebarTable {
            background: rgba(54, 47, 43, 0.96);
            border: 1px solid rgba(244, 225, 185, 0.18);
            border-radius: 16px;
            alternate-background-color: rgba(67, 59, 54, 0.98);
            color: #fff8ed;
            gridline-color: rgba(244, 225, 185, 0.09);
            font-size: 12px;
            selection-background-color: rgba(208, 175, 104, 0.28);
        }
        QTableWidget#sidebarTable::item {
            padding: 6px 8px;
        }
        QTableWidget#sidebarTable QHeaderView::section {
            background: rgba(244, 231, 205, 0.96);
            color: #33261b;
            border-right: 1px solid rgba(184, 151, 92, 0.2);
            padding: 8px;
            font-weight: 900;
        }
        QPushButton {
            background: #272221;
            color: #f7edd7;
            border: 0;
            border-radius: 16px;
            padding: 12px 22px;
            font-weight: 800;
        }
        QPushButton:hover {
            background: #3a312c;
        }
        QPushButton:pressed {
            background: #1f1b1c;
        }
        QPushButton:focus, QLineEdit:focus, QComboBox:focus, QCheckBox:focus {
            border: 2px solid #d0af68;
        }
        QPushButton:disabled {
            background: #8d877f;
            color: #f2ece3;
        }
        QPushButton#primaryButton {
            background: #d0af68;
            color: #1f1b17;
        }
        QPushButton#primaryButton:hover {
            background: #ddb86b;
        }
        QPushButton#secondaryButton {
            background: #302824;
            color: #f8ecd0;
        }
        QPushButton#secondaryButton:hover,
        QPushButton#sidebarButton:hover {
            background: #40342d;
        }
        QPushButton#dangerButton {
            background: #a83232;
            color: #fff7ed;
        }
        QPushButton#dangerButton:hover {
            background: #c44747;
        }
        QPushButton#sidebarButton {
            background: rgba(255, 248, 236, 0.12);
            color: #f7ead0;
            border: 1px solid rgba(223, 209, 178, 0.16);
            min-height: 42px;
        }
        QPushButton#logFilterButton {
            background: rgba(255, 250, 241, 0.08);
            color: #fff9ef;
            border: 1px solid rgba(244, 225, 185, 0.16);
            border-radius: 14px;
            padding: 9px 12px;
            font-size: 12px;
            font-weight: 800;
            text-align: center;
        }
        QPushButton#logFilterButton:checked {
            background: #d0af68;
            color: #1f1b17;
            border-color: #d0af68;
        }
        #logFilterShell {
            background: transparent;
        }
        QTabWidget::pane {
            border: none;
            background: transparent;
        }
        QTabBar::tab {
            background: #eadfc7;
            color: #4a3a29;
            border: 1px solid #dbcaa6;
            padding: 10px 12px;
            min-width: 0;
            border-top-left-radius: 16px;
            border-top-right-radius: 16px;
            margin-right: 6px;
            font-size: 13px;
            font-weight: 800;
        }
        QTabBar::tab:selected {
            background: #fffdfa;
            color: #251f1a;
            border-bottom-color: #fffdfa;
        }
        #logShell {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #1d1a19, stop:1 #272221);
            border-radius: 28px;
        }
        #logTitle {
            color: #fff7ea;
            font-size: 22px;
            font-weight: 800;
        }
        #logSubtitle {
            color: #f1e5d4;
            font-size: 13px;
            line-height: 1.45;
        }
        #logSurface {
            background: #262121;
            border: 1px solid rgba(224, 205, 170, 0.16);
            border-radius: 20px;
        }
        #logOutput {
            background: transparent;
            border: 0;
            color: #f4efe7;
            padding: 8px;
            selection-background-color: #8f7238;
            font-family: 'Cascadia Code';
            font-size: 13px;
        }
        #reportDetail {
            background: #fffdfa;
            border: 1px solid #e1d1ae;
            border-radius: 18px;
            color: #30261d;
            padding: 10px;
            selection-background-color: #e9d5a4;
            font-family: 'Cascadia Code';
            font-size: 12px;
        }
        QTableWidget {
            background: #fffdfa;
            border: 1px solid #e2d0aa;
            border-radius: 18px;
            padding: 0;
            alternate-background-color: #fbf7ef;
            gridline-color: #efe3ca;
        }
        QHeaderView::section {
            background: #f1e6cf;
            color: #473728;
            border: none;
            border-right: 1px solid #dfcfaf;
            padding: 10px;
            font-weight: 800;
        }
        QScrollBar:vertical {
            background: transparent;
            width: 10px;
            margin: 4px 2px 4px 2px;
        }
        QScrollBar::handle:vertical {
            background: rgba(150, 123, 78, 0.55);
            border-radius: 5px;
            min-height: 42px;
        }
        QScrollBar::handle:vertical:hover {
            background: rgba(193, 158, 99, 0.82);
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: transparent;
            height: 0;
        }
        QScrollBar:horizontal {
            background: transparent;
            height: 10px;
            margin: 2px 4px 2px 4px;
        }
        QScrollBar::handle:horizontal {
            background: rgba(150, 123, 78, 0.55);
            border-radius: 5px;
            min-width: 42px;
        }
        QScrollBar::handle:horizontal:hover {
            background: rgba(193, 158, 99, 0.82);
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: transparent;
            width: 0;
        }
        """

    def message_box_stylesheet(self) -> str:
        return """
        QMessageBox {
            background: #1e1a18;
        }
        QMessageBox QLabel {
            color: #fff8ef;
            font-family: 'Segoe UI';
            font-size: 15px;
        }
        QMessageBox QPushButton {
            min-width: 108px;
            min-height: 40px;
            border-radius: 14px;
            background: #3a302b;
            color: #fff6e8;
            padding: 8px 16px;
            font-weight: 800;
            border: 1px solid rgba(244, 225, 185, 0.12);
        }
        QMessageBox QPushButton:hover {
            background: #4a3d36;
        }
        QMessageBox QPushButton:default {
            background: #d0af68;
            color: #1f1b17;
            border-color: #d0af68;
        }
        """

    def show_message(self, title: str, text: str, level: str) -> None:
        self.raise_()
        self.activateWindow()
        box = QMessageBox(self)
        box.setWindowTitle(repair_display_text(title))
        box.setText(repair_display_text(text))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
        box.setStyleSheet(self.message_box_stylesheet())
        if level == "error":
            box.setIcon(QMessageBox.Icon.Critical)
        elif level == "warning":
            box.setIcon(QMessageBox.Icon.Warning)
        else:
            box.setIcon(QMessageBox.Icon.Information)
        box.setWindowFlags(box.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        box.exec()

    def ask_confirmation(self, title: str, text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(repair_display_text(title))
        box.setText(repair_display_text(text))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setStyleSheet(self.message_box_stylesheet())
        return box.exec() == QMessageBox.StandardButton.Yes

    def append_log(self, message: str) -> None:
        message = repair_display_text(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        level = self.infer_log_level(message)
        labels = {
            "all": "TÜMÜ",
            "info": "BİLGİ",
            "success": "BAŞARILI",
            "warning": "UYARI",
            "error": "HATA",
        }
        self.log_entries.append(
            {
                "time": timestamp,
                "text": message,
                "level": level,
                "label": labels.get(level, "BİLGİ"),
            }
        )
        # Python listesinde son 5000 kaydı tutarak bellek şişmesini engelle
        if len(self.log_entries) > 5000:
            self.log_entries.pop(0)

        if self.task_busy:
            self.activity_detail.setText(message)
        self.update_step_progress_from_log(message)
        self.refresh_log_view()

    def update_activity_elapsed(self) -> None:
        if self.busy_started_at is None:
            self.activity_elapsed.setText("00:00")
            return
        seconds = max(0, int((datetime.now() - self.busy_started_at).total_seconds()))
        self.activity_elapsed.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")

    def set_busy(self, busy: bool) -> None:
        self.task_busy = busy
        if busy:
            self.busy_started_at = datetime.now()
            self.activity_title.setText(self.current_task_name or "İşlem devam ediyor")
            self.activity_detail.setText("Hazırlanıyor...")
            self.activity_elapsed.setText("00:00")
            self.activity_frame.show()
            self.activity_timer.start()
        else:
            self.activity_timer.stop()
            self.busy_started_at = None
            self.activity_frame.hide()
            self.current_task_name = ""

        if self.main_tabs is not None:
            self.main_tabs.setEnabled(not busy)
        for button in (
            self.preflight_button,
            self.apply_profile_button,
            self.generate_button,
            self.create_button,
            self.domain_leave_button,
            self.backup_button,
            self.settings_button,
            self.compact_settings_button,
            self.report_delete_button,
            self.report_refresh_button,
        ):
            button.setEnabled(not busy)
        for button in self.checkboxes.values():
            button.setEnabled(not busy)
        if not busy:
            self.apply_profile_button.setEnabled(self.profile_combo.count() > 0)
            self.refresh_option_states()

    def generate_identity(self) -> None:
        if not self.apply_company_password(self.company_combo.currentText()):
            self.show_message(
                "Uyarı",
                "Seçilen şirket için kayıtlı parola yok. Ayarlar ekranından parola tanımlayın.",
                "warning",
            )
            return
        try:
            result = self.service.generate_identity(
                self.full_name_input.text(),
                self.company_combo.currentText(),
            )
        except Exception as exc:  # noqa: BLE001
            self.show_message("Uyarı", str(exc), "warning")
            return

        self.username_output.setText(result.username)
        self.pc_name_output.setText(result.computer_name)
        self.password_output.setText(result.password)
        self.update_summary()
        self.reset_step_statuses_from_ui()
        self.append_log("Kullanıcı adı, PC adı ve varsayılan şifre üretildi.")

    def apply_company_password(self, company_name: str) -> bool:
        """Keep the password field tied to the company selection, never stale."""
        selected_name = company_name.strip()
        try:
            company = self.service.resolve_company(selected_name)
        except Exception:
            self.password_output.clear()
            self.company_password_status.setText("Şirket seçildiğinde kayıtlı parola otomatik gelecektir.")
            return False

        password = company.password.strip()
        if password:
            self.password_output.setText(password)
            self.company_password_status.setText("Kayıtlı şirket parolası otomatik uygulandı.")
            return True

        # Do not leave the previous company's password in the form when this
        # company has no configured credential.
        self.password_output.clear()
        self.company_password_status.setText(
            "Bu şirket için kayıtlı parola yok; Ayarlar ekranından tanımlayın."
        )
        return False

    def auto_generate_identity(self) -> None:
        full_name = self.full_name_input.text().strip()
        company_name = self.company_combo.currentText().strip()
        has_company_password = self.apply_company_password(company_name)

        if not full_name:
            self.username_output.setText("")
            self.pc_name_output.setText("")
            self.update_summary()
            return
        if not has_company_password:
            self.username_output.setText("")
            self.pc_name_output.setText("")
            self.update_summary()
            return
        try:
            result = self.service.generate_identity(full_name, company_name)
            self.username_output.setText(result.username)
            self.pc_name_output.setText(result.computer_name)
            self.password_output.setText(result.password)
            self.update_summary()
        except Exception:
            pass

    def build_request(self) -> OnboardingRequest:
        """Ekrandaki son durumu servisin anlayacagi tek istek nesnesine toplar."""
        if not self.username_output.text().strip():
            self.generate_identity()

        request = OnboardingRequest(
            profile_name=self.active_profile_name,
            full_name=self.full_name_input.text().strip(),
            company_name=self.company_combo.currentText().strip(),
            user_type=self.user_type_combo.currentText().strip(),
            username=self.username_output.text().strip(),
            computer_name=self.pc_name_output.text().strip(),
            password=self.password_output.text().strip(),
            options={name: checkbox.isChecked() for name, checkbox in self.checkboxes.items()},
        )
        self.service.validate_request(request)
        return request

    def run_background_task(
        self,
        task: Callable[[Callable[[str], None]], object],
        on_finished: Callable[[object], None] | None = None,
        task_name: str = "Arka plan işlemi",
    ) -> None:
        """UI'yi kilitlemeden servis gorevi calistirir.

        Neden gerekli:
        - Domain
        - AnyDesk
        - ESET
        - PowerShell
        gibi adimlar bazen yavas olur. Ana thread bloke olursa pencere donar.
        """
        if self.current_thread is not None and self.current_thread.isRunning():
            self.show_message(
                "İşlem Devam Ediyor",
                "Başka bir işlem halen çalışıyor. Tamamlanmasını bekledikten sonra tekrar deneyin.",
                "warning",
            )
            return
        if self.app_check_running:
            self.show_message(
                "Uygulama Taraması",
                "Kurulu uygulama taraması tamamlanmadan yeni bir işlem başlatılamaz.",
                "warning",
            )
            return
        self.current_task_name = task_name
        self.current_finish_handler = on_finished
        self.set_busy(True)
        self.current_thread = QThread(self)
        self.current_worker = TaskWorker(task)
        self.current_worker.moveToThread(self.current_thread)

        self.current_thread.started.connect(self.current_worker.run)
        self.current_worker.log.connect(self.append_log)
        self.current_worker.failed.connect(self._handle_failure)
        # A lambda without a QObject receiver can run in the worker thread.
        # UI callbacks (including the recovery-card actions) must always be
        # delivered to this MainWindow on the GUI thread.
        self.current_worker.finished.connect(self._handle_worker_finished)
        self.current_worker.finished.connect(self.current_thread.quit)
        self.current_worker.failed.connect(self.current_thread.quit)
        self.current_thread.finished.connect(self._on_task_thread_finished)
        self.current_thread.finished.connect(self.current_worker.deleteLater)
        self.current_thread.finished.connect(self.current_thread.deleteLater)
        self.current_thread.start()

    def _handle_failure(self, message: str) -> None:
        if self.current_running_step:
            self.set_step_status(self.current_running_step, "Hata", message)
            self.current_running_step = ""
        self.append_log(f"Kritik hata: {message}")
        if self.current_task_name != "Cihaz kurulumu devam ediyor":
            # The main onboarding flow already writes a structured report. For
            # other background actions, save a tiny diagnostic only when this
            # portable package is actually running from the USB drive.
            self.service.export_task_failure_to_usb(
                self.current_task_name or "Arka plan islemi",
                message,
                self.append_log,
            )
        self.refresh_report_history()
        if (
            self.current_task_name == "Cihaz kurulumu devam ediyor"
            and message.startswith("On kontrol basarisiz:")
            and self.last_onboarding_request is not None
        ):
            self._offer_skip_preflight_and_continue(message)
            return
        self.show_message("Hata", message, "error")

    def _offer_skip_preflight_and_continue(self, message: str) -> None:
        """Let the operator explicitly bypass preflight warnings once.

        Individual setup steps remain responsible for their own validation;
        this choice never suppresses runtime errors or safety checks.
        """
        box = QMessageBox(self)
        box.setWindowTitle("On Kontrol Uyarisi")
        box.setText(message)
        box.setInformativeText(
            "Uyarilari atlayip kuruluma devam edebilirsiniz. "
            "Her kurulum adiminin kendi kontrolu yine calisir."
        )
        box.setIcon(QMessageBox.Icon.Warning)
        skip_button = box.addButton("Atla ve Kuruluma Devam Et", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Kurulumu Durdur", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(skip_button)
        box.setStyleSheet(self.message_box_stylesheet())
        box.exec()
        if box.clickedButton() is not skip_button:
            return

        request = self.last_onboarding_request
        request.options["_skip_preflight_once"] = True
        self.append_log("Operator on kontrol uyarilarini atladi; kurulum yeniden baslatiliyor.")
        # `failed` is emitted before the worker thread has fully stopped.
        # Queue the retry so it cannot collide with that terminating thread.
        QTimer.singleShot(100, self._resume_onboarding_after_preflight_skip)

    def _resume_onboarding_after_preflight_skip(self) -> None:
        # Wait for `_on_task_thread_finished` to clear the old thread object.
        # Starting earlier could let its delayed finished signal reset the new
        # task's UI state.
        if self.current_thread is not None:
            QTimer.singleShot(100, self._resume_onboarding_after_preflight_skip)
            return
        request = self.last_onboarding_request
        if request is None:
            return
        self.run_background_task(
            lambda log: self.service.apply_onboarding(request, log),
            self.show_onboarding_result_messages,
            "Cihaz kurulumu devam ediyor",
        )

    def _handle_finish(self, result: object, on_finished: Callable[[object], None] | None) -> None:
        if on_finished:
            on_finished(result)

    @Slot(object)
    def _handle_worker_finished(self, result: object) -> None:
        self._handle_finish(result, self.current_finish_handler)

    def _on_task_thread_finished(self) -> None:
        self.current_worker = None
        self.current_thread = None
        self.current_finish_handler = None
        self.set_busy(False)

    def closeEvent(self, event) -> None:
        critical_running = self.current_thread is not None and self.current_thread.isRunning()
        scan_running = self.app_check_running
        if critical_running or scan_running:
            self.show_message(
                "İşlem Devam Ediyor",
                "Arka plan işlemi tamamlanmadan pencere kapatılamaz. Bu koruma kurulumun yarım kalmasını önler.",
                "warning",
            )
            event.ignore()
            return
        event.accept()

    def show_onboarding_result_messages(self, messages: object) -> None:
        self.load_last_preflight()
        self.refresh_report_history()
        self.update_summary()
        
        # Raporlar sekmesine otomatik gecis yap (onemli gelismeler goz onunde acik sekilde dursun)
        if self.main_tabs:
            report_index = self.main_tabs.indexOf(self.reports_tab)
            if report_index >= 0:
                self.main_tabs.setCurrentIndex(report_index)
        if self.report_table.rowCount() > 0:
            self.report_table.selectRow(0)
            self.load_selected_report_detail()

        if not isinstance(messages, list):
            return
        
        def display():
            for title, text, level in messages:
                # Informational modal dialogs delayed the restart countdown
                # until every message was manually dismissed. The report is
                # already selected, so keep the full result in the live log.
                self.append_log(f"{title}: {text}")
            
            request = self.last_onboarding_request
            options = request.options if request is not None else {}
            local_standard_wallpaper = bool(
                request
                and request.user_type == "Lokal"
                and options.get("desktop_wallpaper")
                and not options.get("administrator")
            )
            has_deferred = any(
                options.get(name)
                for name in (
                    "main_file_server",
                    "network_printer",
                    "desktop_signature",
                    "classic_outlook",
                    "eset",
                    "windows_update",
                )
            ) or local_standard_wallpaper
            is_rebooting = bool(
                request
                and (
                    request.user_type == "Domain"
                    or options.get("restart")
                    or options.get("delete_x_user")
                    or has_deferred
                )
            )

            if options.get("_initial_restart_scheduled"):
                self.set_step_status(
                    "Yeniden baslat",
                    "Planlandi",
                    "SYSTEM gorevi rapor yazimini tamamlayip otomatik yeniden baslatacak.",
                )
                self.append_log("Yeniden baslatma arayuz sayacina bagli degil; SYSTEM gorevi ile otomatik yapilacak.")
                return

            if options.get("delete_x_user"):
                self.set_step_status(
                    "x kullanicisi temizligi",
                    "Devam ediyor",
                    "SYSTEM gorevi kurulumun son adiminda planlandi; X hesabi ve profili dogrulanarak silinecek.",
                )
                self.append_log("X temizleme SYSTEM gorevi planlandi; dogrulama sonrasi bilgisayar otomatik yeniden baslatilacak.")
                return

            if is_rebooting:
                dialog = CountdownDialog(
                    self,
                    timeout_seconds=60,
                    allow_cancel=True,
                )
                decision = dialog.exec()
                if decision == QDialog.DialogCode.Accepted:
                    self.append_log("Bilgisayar yeniden baslatiliyor...")
                    import subprocess
                    subprocess.run(["shutdown.exe", "/r", "/t", "0", "/f"], capture_output=True)
                else:
                    self.append_log("Kullanici tarafindan otomatik yeniden baslatma iptal edildi.")
                    self.show_message("Bilgi", "Otomatik yeniden başlatma iptal edildi. Yapılan değişikliklerin tam olarak uygulanabilmesi için lütfen bilgisayarı daha sonra manuel olarak yeniden başlatın.", "info")
            else:
                self.show_message(
                    "Kurulum Tamamlandı",
                    "Seçili kurulum adımları tamamlandı. Raporu inceleyebilirsiniz.",
                    "info"
                )
        
        QTimer.singleShot(100, display)

    def show_result_messages(self, messages: object) -> None:
        self.load_last_preflight()
        self.refresh_report_history()
        self.update_summary()
        if not isinstance(messages, list) or not messages:
            return
        
        # Post-login tamamlandığı için Raporlar sekmesine geçiş yap ve en güncel raporu seç
        if self.main_tabs:
            report_index = self.main_tabs.indexOf(self.reports_tab)
            if report_index >= 0:
                self.main_tabs.setCurrentIndex(report_index)
            if self.report_table.rowCount() > 0:
                self.report_table.selectRow(0)
                self.load_selected_report_detail()
        
        def display():
            for title, text, level in messages:
                self.show_message(title, text, level)
        
        QTimer.singleShot(100, display)

    def run_startup_tasks(self) -> None:
        self.append_log("Başlangıç kontrolleri yapılıyor.")
        # Geçici temizlik scriptini temizle
        try:
            cleanup_script = Path("C:/AcikCleanup.ps1")
            if cleanup_script.exists():
                cleanup_script.unlink(missing_ok=True)
                self.append_log("Eski temizlik betiği (C:\\AcikCleanup.ps1) temizlendi.")
        except Exception as exc:
            self.append_log(f"Eski temizlik betiği temizlenirken hata: {exc}")

        def startup_finished(messages: object) -> None:
            self.show_result_messages(messages)
            self.start_app_installation_check()

        self.run_background_task(
            self.service.handle_deferred_startup,
            startup_finished,
            "Bekleyen kurulum adımları kontrol ediliyor",
        )

    def start_app_installation_check(self) -> None:
        if self.app_check_running:
            return

        self.append_log("Kurulu uygulamaların tespiti başlatıldı.")
        self.app_check_running = True
        self.app_check_queue = [
            "eset", "anydesk", "chrome", "forticlient",
            "office", "jre", "winrar", "hackbgrt",
        ]
        # Kontroller kısa dosya/Registry sorgularıdır. Her birini ayrı event-loop
        # turunda yapmak pencereyi duyarlı tutar ve bazı Intel/uzak ekran
        # sürücülerinde görülen kısa ömürlü ikinci QThread çökmesini önler.
        QTimer.singleShot(0, self._check_next_installed_app)

    @Slot()
    def _check_next_installed_app(self) -> None:
        if not self.app_check_queue:
            self.app_check_running = False
            self.append_log("Kurulu uygulama taraması tamamlandı.")
            return

        program_name = self.app_check_queue.pop(0)
        try:
            installed: bool | None = self.service.is_program_installed(program_name)
        except Exception:
            installed = None
        self.on_app_checked(program_name, installed)
        if self.app_check_queue:
            QTimer.singleShot(0, self._check_next_installed_app)
        else:
            # Son kontrolün ardından yeni bir zero-timer beklemek bazı Qt
            # sürümlerinde olayı düşürebiliyor ve tarama bayrağı açık kalıyor.
            self.app_check_running = False
            self.append_log("Kurulu uygulama taraması tamamlandı.")

    @Slot(str, object)
    def on_app_checked(self, program_name: str, installed: object) -> None:
        if installed is None:
            status_text = "kontrol edilemedi"
        else:
            status_text = "kurulu" if installed else "kurulu değil"
        self.append_log(f"Uygulama kontrolü: {program_name} -> {status_text}")

        # Status etiketini güncelle
        lbl = self.usb_status_labels.get(program_name)
        if lbl:
            if installed is None:
                lbl.setText("Kontrol Edilemedi")
                lbl.setStyleSheet("color: #b26a00; font-size: 12px; font-weight: bold;")
            elif installed:
                lbl.setText("✔ Kurulu")
                lbl.setStyleSheet("color: #2e7d32; font-size: 12px; font-weight: bold;")
            else:
                lbl.setText("✗ Kurulu Değil")
                lbl.setStyleSheet("color: #c62828; font-size: 12px; font-weight: bold;")

        # Tekli kurulum butonunu aktif/deaktif et
        btn = self.usb_install_buttons.get(program_name)
        if btn:
            btn.setEnabled(installed is False)
            if installed is None:
                btn.setText("Yeniden Tara")
            elif installed:
                btn.setText("Kuruldu")
                btn.setStyleSheet("font-size: 11px; padding: 2px 4px; border-radius: 6px; background-color: #e0e0e0; color: #888; border: 1px solid #ccc;")
            else:
                btn.setText("Kur")
                btn.setStyleSheet("font-size: 11px; padding: 2px 4px; border-radius: 6px; background-color: #efdfba; color: #1f1b18; border: 1px solid #d4c19a;")

        connect_button = self.usb_connect_buttons.get(program_name)
        if connect_button:
            connect_button.setEnabled(installed is True)

        if installed:
            cb = self.usb_checkboxes.get(program_name)
            if cb:
                font = cb.font()
                font.setStrikeOut(True)
                cb.setFont(font)
                cb.setToolTip("Bu program zaten bilgisayarda yüklü.")
                cb.setChecked(False)
            


    def start_preflight(self) -> None:
        try:
            request = self.build_request()
        except Exception as exc:  # noqa: BLE001
            self.show_message("Ön Kontrol", str(exc), "warning")
            return

        if not request.full_name:
            self.show_message("Ön Kontrol", "Ad soyad alanını doldurup kimlik bilgilerini üretmek gerekiyor.", "warning")
            return

        self.append_log("Ön kontrol akışı başlatıldı.")
        self.run_background_task(
            lambda log: self.service.run_preflight(request, log),
            self.show_result_messages,
            "Sistem ön kontrolü yapılıyor",
        )

    def start_onboarding(self) -> None:
        if self.refresh_active_workflow_card():
            if self.onboarding_scroll_area is not None:
                self.onboarding_scroll_area.verticalScrollBar().setValue(0)
            self.append_log(
                "Yeni kurulum baslatilmadi; onceki kurulum icin ustteki secimlerden birini kullanin."
            )
            return
        self.set_log_filter("important")
        try:
            request = self.build_request()
        except Exception as exc:  # noqa: BLE001
            self.show_message("Hata", str(exc), "error")
            return

        if not request.full_name:
            self.show_message("Uyarı", "Ad soyad alanı boş olamaz.", "warning")
            return
        self.last_onboarding_request = request

        risky_options = [
            OPTION_LABELS[name]
            for name in (
                "delete_x_user",
                "administrator",
                "windows_activation",
                "hackbgrt",
                "restart",
            )
            if request.options.get(name)
        ]
        if risky_options:
            confirmed = self.ask_confirmation(
                "Kurulum Onayı",
                "Aşağıdaki kritik işlemler seçili:\n\n- "
                + "\n- ".join(risky_options)
                + "\n\nÖn kontrol yeniden çalıştırılacak. Bu planla devam edilsin mi?",
            )
            if not confirmed:
                return

        self.append_log("Kurulum akışı başlatıldı.")
        self.log_expanded_for_installation = True
        self.refresh_responsive_layout()
        self.reset_step_statuses(self.build_expected_steps(request))
        self.run_background_task(
            lambda log: self.service.apply_onboarding(request, log),
            self.show_onboarding_result_messages,
            "Cihaz kurulumu devam ediyor",
        )

    def start_domain_leave(self) -> None:
        if not self.service.is_admin_session():
            self.show_message(
                "Yonetici Yetkisi Gerekli",
                "Domainden cikis yalnizca yonetici olarak acilan uygulamada calistirilabilir.",
                "warning",
            )
            return
        self.run_background_task(
            lambda log: self.service.leave_domain(self.config.domain.username, log),
            self._on_domain_leave_requested,
            "Domainden cikis planlaniyor",
        )

    def _on_domain_leave_requested(self, previous_domain: object) -> None:
        domain_name = str(previous_domain).strip()
        if not domain_name:
            self.append_log("Cihaz zaten domaine uye degil; yeniden baslatma istenmedi.")
            self.show_message(
                "Domain Durumu",
                "Cihaz zaten bir domaine uye degil. Yeniden baslatma istenmedi.",
                "info",
            )
            return
        self.append_log(
            f"Domainden cikis planlandi: {domain_name}. Cihaz 15 saniye icinde yeniden baslatilacak."
        )
        self.show_message(
            "Domainden Cikis Planlandi",
            "Cihaz workgroup'a gecmek uzere domaine cikis istegini kabul etti. "
            "Yerel kullanici secimi geri yuklendi; cihaz 15 saniye icinde yeniden baslatilacak.",
            "info",
        )

    def start_terminate(self) -> None:
        confirmed = self.ask_confirmation(
            "Süreci Sonlandır",
            "Askıda kalmış olabilecek (kitlenmiş) bir kurulum süreci varsa sonlandırılacak ve temizlenecektir.\nDevam etmek istiyor musunuz?"
        )
        if not confirmed:
            return

        self.append_log("Askıdaki kurulum ve planlanan görevler arka planda sonlandırılıyor.")
        self.run_background_task(
            lambda log: self.service.cancel_active_workflow(log),
            self._on_manual_termination_finished,
            "Askıdaki kurulum sonlandırılıyor",
        )

    def _on_manual_termination_finished(self, _result: object) -> None:
        self.refresh_active_workflow_card()
        self.refresh_report_history()
        self.update_summary()
        self.append_log("Varsa askıdaki süreç sonlandırıldı ve temizlendi.")
        self.show_message(
            "Başarılı",
            "Varsa askıdaki süreç başarıyla sonlandırıldı ve temizlendi.",
            "info",
        )

    def start_backup(self) -> None:
        source_dir = self.backup_source_input.text().strip()
        destination = self.destination_input.text().strip()
        self.append_log("Profil yedekleme akışı başlatıldı.")
        self.run_background_task(
            lambda log: self.service.backup_profile_custom(source_dir, destination, log),
            self.show_result_messages,
            "Profil yedekleniyor",
        )

    def pick_backup_source(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Kaynak Klasörü Seç",
            self.backup_source_input.text().strip() or "C:\\Users",
        )
        if directory:
            self.backup_source_input.setText(directory)

    def on_backup_user_combo_changed(self, text: str) -> None:
        username = text.strip()
        if username:
            self.backup_source_input.setText(f"C:\\Users\\{username}")
        else:
            self.backup_source_input.setText("")

    def populate_backup_users(self) -> None:
        self.backup_user_combo.blockSignals(True)
        self.backup_user_combo.clear()
        self.backup_user_combo.addItem("")
        
        users_dir = Path("C:/Users")
        if users_dir.exists():
            try:
                exclude = {"public", "default", "default user", "all users", "desktop.ini"}
                for entry in users_dir.iterdir():
                    if entry.is_dir() and entry.name.lower() not in exclude:
                        self.backup_user_combo.addItem(entry.name)
            except Exception:
                pass
        self.backup_user_combo.blockSignals(False)

    def pick_destination(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Hedef Klasörü Seç",
            self.destination_input.text().strip() or str(Path.home()),
        )
        if directory:
            self.destination_input.setText(directory)

    def request_settings_access(self, action_text: str = "ayarları açmak") -> bool:
        remaining_lock = int(self.settings_locked_until - time.monotonic())
        if remaining_lock > 0:
            self.show_message(
                "Ayarlar Kilitli",
                f"Çok sayıda hatalı deneme yapıldı. {remaining_lock + 1} saniye sonra tekrar deneyin.",
                "warning",
            )
            return False
        if self.settings_locked_until:
            # The 60-second lockout has expired. SECURITY.md documents this as
            # a fresh five-attempt window, not "one more try before an
            # immediate re-lock" - reset the counter along with the deadline.
            self.settings_failed_attempts = 0
            self.settings_locked_until = 0.0

        password_dialog = SettingsPasswordDialog(self)
        if password_dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        if password_dialog.is_valid_password():
            self.settings_failed_attempts = 0
            self.settings_locked_until = 0.0
            return True

        self.settings_failed_attempts += 1
        attempts_left = max(0, 5 - self.settings_failed_attempts)
        self.append_log(f"Ayarlar erişimi reddedildi: {action_text} için parola doğrulanamadı.")
        if attempts_left == 0:
            self.settings_locked_until = time.monotonic() + 60
            self.show_message(
                "Ayarlar Kilitli",
                "Beş hatalı deneme nedeniyle ayar erişimi 60 saniye kilitlendi.",
                "warning",
            )
        else:
            self.show_message(
                "Ayarlar",
                f"Şifre hatalı. Kısa süreli kilitten önce {attempts_left} deneme kaldı.",
                "warning",
            )
        return False

    def open_settings(self) -> None:
        if not self.service.is_admin_session():
            self.show_message(
                "Ayarlar",
                "Ayarlar yalnızca yükseltilmiş yönetici oturumunda değiştirilebilir.",
                "warning",
            )
            return
        if not self.request_settings_access():
            return

        self.settings_dialog = SettingsDialog(self.config, self)
        self.settings_dialog.setModal(True)
        
        def on_settings_accepted():
            if self.settings_dialog.saved_config is not None:
                self.config = self.settings_dialog.saved_config
                self.service = OnboardingService(self.config)
                self.apply_config_to_widgets()
                self.append_log("Ayarlar kaydedildi ve arayüze yansıtıldı.")
                self.show_message("Ayarlar", "Ayarlar kaydedildi.", "info")

        self.settings_dialog.accepted.connect(on_settings_accepted)
        self.settings_dialog.show()

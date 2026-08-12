"""
ACIK Kurulum ayar modeli ve JSON <-> Python donusum katmani.

Stajyer Notu:
- Uygulamadaki sabitlerin buyuk kismi burada tanimlanan dataclass alanlarindan gelir.
- Yeni bir ozellik eklerken genellikle 3 yere dokunursunuz:
  1. Ilgili dataclass'a alan eklemek
  2. `load_app_config()` icinde JSON'dan okumak
  3. `app_config_to_dict()` icinde tekrar yazmak
- Boylesi bir yapi UI ile is mantigini ayar dosyasindan ayirir.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def repair_legacy_display_text(value: str) -> str:
    """Repair only human-facing legacy text read from portable JSON files.

    Passwords and other operational fields deliberately do not pass through
    this function.  Company and profile names, notes, and branding are safe
    display identifiers and must render Turkish characters consistently.
    """
    repaired = value
    for _ in range(3):
        if not any(marker in repaired for marker in ("\u00c3", "\u00c2", "\u00e2", "\ufffd")):
            break
        try:
            candidate = repaired.encode("cp1252").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            break
        if candidate == repaired:
            break
        repaired = candidate
    return repaired


@dataclass(slots=True)
class BrandingConfig:
    title: str
    subtitle: str
    logo_path: Path


@dataclass(slots=True)
class CompanyProfile:
    prefix: str
    password: str


@dataclass(slots=True)
class StatePaths:
    domain_log_path: Path
    eset_log_path: Path
    user_cleanup_log_path: Path


@dataclass(slots=True)
class DomainConfig:
    name: str
    username: str
    password: str


@dataclass(slots=True)
class WifiProfile:
    ssid: str
    password: str


@dataclass(slots=True)
class BackupConfig:
    network_path: str
    network_user: str
    network_password: str
    folders: list[str]


@dataclass(slots=True)
class WorkflowProfile:
    user_type: str
    company_name: str
    note: str
    options: dict[str, bool]


@dataclass(slots=True)
class NetworkResourceConfig:
    credential_domain: str
    required_wifi_ssid: str
    printer_host: str
    printer_share: str
    file_server_host: str
    file_server_share: str
    file_server_shortcut_name: str


@dataclass(slots=True)
class ToolConfig:
    local_admin_username: str
    local_admin_password: str
    anydesk_install_dir: str
    anydesk_installer_path: str
    eset_installer_path: str
    hackbgrt_setup_path: str


@dataclass(slots=True)
class ReportingConfig:
    enabled: bool
    output_dir: Path
    webhook_url: str
    webhook_token: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass(slots=True)
class DesktopAutomationConfig:
    signature_source_dir: Path
    signature_folder_name: str
    outlook_classic_path: str
    outlook_email: str
    outlook_password: str
    wallpaper_source_path: Path
    wallpaper_target_path: Path
    lock_screen_source_path: Path
    lock_screen_target_path: Path
    wallpaper_lock_standard_users: bool


@dataclass(slots=True)
class WindowsConfig:
    activation_product_key: str
    restart_delay_seconds: int
    update_uri: str


@dataclass(slots=True)
class AppConfig:
    """Uygulamanin tum konfigurasyonunu tek nesnede toplar."""
    base_dir: Path
    config_path: Path
    branding: BrandingConfig
    user_types: list[str]
    companies: dict[str, CompanyProfile]
    profiles: dict[str, WorkflowProfile]
    state_paths: StatePaths
    domain: DomainConfig
    wifi_profiles: dict[str, WifiProfile]
    backup: BackupConfig
    network_resources: NetworkResourceConfig
    tools: ToolConfig
    reporting: ReportingConfig
    desktop_automation: DesktopAutomationConfig
    windows: WindowsConfig
    legacy_cleanup_user: str


def _resolve_path(base_dir: Path, raw: str) -> Path:
    raw_path = Path(raw)
    if raw_path.is_absolute():
        return raw_path
    return (base_dir / raw).resolve()


def _serialize_path(base_dir: Path, raw: Path) -> str:
    try:
        relative = raw.resolve().relative_to(base_dir.resolve())
    except ValueError:
        return raw.as_posix()
    return relative.as_posix()


def _resolve_optional_path(base_dir: Path, raw: str) -> Path:
    if not raw.strip():
        return Path()
    return _resolve_path(base_dir, raw)


def _resolve_packaged_asset_or_configured(base_dir: Path, raw: str, asset_name: str) -> Path:
    """Prefer the release's own asset so a USB drive never follows an old C: path."""
    packaged_asset = (base_dir / "assets" / asset_name).resolve()
    if packaged_asset.is_file():
        return packaged_asset
    return _resolve_optional_path(base_dir, raw)


def _serialize_optional_path(base_dir: Path, raw: Path) -> str:
    if not str(raw) or str(raw) == ".":
        return ""
    return _serialize_path(base_dir, raw)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Konfigürasyon dosyası bulunamadı: {path}") from exc
    except PermissionError as exc:
        raise RuntimeError(
            "Konfigürasyon dosyasına erişilemiyor. Uygulamayı yönetici olarak "
            f"çalıştırın ve dosya izinlerini kontrol edin: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Konfigürasyon dosyası bozuk veya geçersiz JSON içeriyor: {path}") from exc


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """JSON'dan gelen farkli tipleri guvenli sekilde bool'a cevirir.

    Neden gerekli:
    - Bazi saha ayarlari `"true"` veya `"false"` gibi string gelebilir.
    - Duz `bool("false")` Python'da `True` olur; bu tehlikelidir.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "evet", "on"}:
            return True
        if normalized in {"false", "0", "no", "hayir", "hayır", "off", ""}:
            return False
    return default


def load_app_config(base_dir: Path, *, public_only: bool = False) -> AppConfig:
    """Config dosyasini okuyup guclu tipli `AppConfig` nesnesine cevirir.

    Stajyer Notu:
    - Bu fonksiyon projenin "ayar girisi"dir.
    - Bir ozellik UI'da gozukuyor ama calismiyorsa once burada ilgili alanin
      dogru okunup okunmadigina bakmak gerekir.
    """
    explicit_path = os.environ.get("ACIK_CONFIG_PATH", "").strip()
    local_path = base_dir / "app_config.local.json"
    private_path = base_dir.parent / "private_secrets" / "app_config.local.json"
    example_path = base_dir / "app_config.example.json"
    if public_only:
        # The standard-user post-login phase must not need access to the
        # operational config containing domain, Wi-Fi and admin credentials.
        config_path = example_path
        if not config_path.exists():
            raise RuntimeError(f"Genel çalışma ayarı bulunamadı: {config_path}")
    elif explicit_path:
        config_path = Path(explicit_path).expanduser().resolve()
        if not config_path.exists():
            raise RuntimeError(f"ACIK_CONFIG_PATH dosyasi bulunamadi: {config_path}")
    elif local_path.exists():
        config_path = local_path
    elif private_path.exists():
        config_path = private_path
    else:
        config_path = example_path
    raw = _read_json(config_path)

    companies = {
        repair_legacy_display_text(str(name)): CompanyProfile(
            prefix=str(values.get("prefix", "")),
            password=str(values.get("password", "")),
        )
        for name, values in raw.get("companies", {}).items()
    }

    profiles = {
        repair_legacy_display_text(str(name)): WorkflowProfile(
            user_type=str(values.get("user_type", "Lokal")),
            company_name=repair_legacy_display_text(str(values.get("company_name", ""))),
            note=repair_legacy_display_text(str(values.get("note", ""))),
            options={str(key): _coerce_bool(flag) for key, flag in values.get("options", {}).items()},
        )
        for name, values in raw.get("profiles", {}).items()
    }

    wifi_profiles = {
        str(name): WifiProfile(
            ssid=str(values.get("ssid", "")),
            password=str(values.get("password", "")),
        )
        for name, values in raw.get("wifi_profiles", {}).items()
    }

    branding_raw = raw.get("branding", {})
    state_raw = raw.get("state_paths", {})
    domain_raw = raw.get("domain", {})
    backup_raw = raw.get("backup", {})
    network_raw = raw.get("network_resources", {})
    tool_raw = raw.get("tools", {})
    reporting_raw = raw.get("reporting", {})
    desktop_raw = raw.get("desktop_automation", {})
    windows_raw = raw.get("windows", {})
    # These images are portable release assets.  Legacy configurations may
    # still contain C:\\Wallpaper or C:\\ProgramData targets; normalise those
    # values now so no later UI, preflight, or workflow step can follow C:.
    wallpaper_source_path = _resolve_packaged_asset_or_configured(
        base_dir,
        str(desktop_raw.get("wallpaper_source_path", "assets/wallpaper.jpg")),
        "wallpaper.jpg",
    )
    lock_screen_source_path = _resolve_packaged_asset_or_configured(
        base_dir,
        str(desktop_raw.get("lock_screen_source_path", "assets/uyku modu.jpg")),
        "uyku modu.jpg",
    )

    # Domain ayarlarinin tek kaynagi secilen JSON yapilandirmasidir.
    return AppConfig(
        base_dir=base_dir,
        config_path=config_path,
        branding=BrandingConfig(
            title=repair_legacy_display_text(str(branding_raw.get("title", "A.CIK Kurulum"))),
            subtitle=repair_legacy_display_text(str(branding_raw.get("subtitle", ""))),
            logo_path=_resolve_path(base_dir, str(branding_raw.get("logo_path", "assets/acik_logo.png"))),
        ),
        user_types=[str(value) for value in raw.get("user_types", ["Lokal", "Domain"])],
        companies=companies,
        profiles=profiles,
        state_paths=StatePaths(
            domain_log_path=_resolve_path(base_dir, str(state_raw.get("domain_log_path", "runtime/domainLog.txt"))),
            eset_log_path=_resolve_path(base_dir, str(state_raw.get("eset_log_path", "runtime/esetLog.txt"))),
            user_cleanup_log_path=_resolve_path(base_dir, str(state_raw.get("user_cleanup_log_path", "runtime/xLog.txt"))),
        ),
        domain=DomainConfig(
            name=str(domain_raw.get("name", "")),
            username=str(domain_raw.get("username", "")),
            password=str(domain_raw.get("password", "")),
        ),
        wifi_profiles=wifi_profiles,
        backup=BackupConfig(
            network_path=str(backup_raw.get("network_path", "")),
            network_user=str(backup_raw.get("network_user", "")),
            network_password=str(backup_raw.get("network_password", "")),
            folders=[str(value) for value in backup_raw.get("folders", ["Desktop", "Documents", "Pictures", "Videos"])],
        ),
        network_resources=NetworkResourceConfig(
            credential_domain=str(network_raw.get("credential_domain", "ACIK")),
            required_wifi_ssid=str(network_raw.get("required_wifi_ssid", "")),
            printer_host=str(network_raw.get("printer_host", "10.9.10.250")),
            printer_share=str(network_raw.get("printer_share", "")),
            file_server_host=str(network_raw.get("file_server_host", "10.9.10.174")),
            file_server_share=str(network_raw.get("file_server_share", "")),
            file_server_shortcut_name=str(network_raw.get("file_server_shortcut_name", "FileServer")),
        ),
        tools=ToolConfig(
            local_admin_username=str(tool_raw.get("local_admin_username", "lokaladm")),
            local_admin_password=str(tool_raw.get("local_admin_password", "")),
            anydesk_install_dir=str(tool_raw.get("anydesk_install_dir", "C:/Program Files (x86)/AnyDesk")),
            anydesk_installer_path=str(tool_raw.get("anydesk_installer_path", "payloads/anydesk/AnyDesk.exe")),
            eset_installer_path=str(tool_raw.get("eset_installer_path", "")),
            hackbgrt_setup_path=str(tool_raw.get("hackbgrt_setup_path", "")),
        ),
        reporting=ReportingConfig(
            enabled=_coerce_bool(reporting_raw.get("enabled", True), default=True),
            output_dir=_resolve_path(base_dir, str(reporting_raw.get("output_dir", "runtime/reports"))),
            webhook_url=str(reporting_raw.get("webhook_url", "")),
            webhook_token=str(reporting_raw.get("webhook_token", "")),
            telegram_bot_token=str(reporting_raw.get("telegram_bot_token", "")),
            telegram_chat_id=str(reporting_raw.get("telegram_chat_id", "")),
        ),
        desktop_automation=DesktopAutomationConfig(
            signature_source_dir=_resolve_optional_path(base_dir, str(desktop_raw.get("signature_source_dir", ""))),
            signature_folder_name=str(desktop_raw.get("signature_folder_name", "Imza")),
            outlook_classic_path=str(desktop_raw.get("outlook_classic_path", "")),
            outlook_email=str(desktop_raw.get("outlook_email", "")),
            outlook_password=str(desktop_raw.get("outlook_password", "")),
            wallpaper_source_path=wallpaper_source_path,
            wallpaper_target_path=wallpaper_source_path,
            lock_screen_source_path=lock_screen_source_path,
            lock_screen_target_path=lock_screen_source_path,
            wallpaper_lock_standard_users=_coerce_bool(desktop_raw.get("wallpaper_lock_standard_users", True), default=True),
        ),
        windows=WindowsConfig(
            activation_product_key=str(windows_raw.get("activation_product_key", "")),
            restart_delay_seconds=int(windows_raw.get("restart_delay_seconds", 20)),
            update_uri=str(windows_raw.get("update_uri", "ms-settings:windowsupdate")),
        ),
        legacy_cleanup_user=str(raw.get("legacy_cleanup_user", "x")),
    )


def app_config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Kaydedilecek config'i tekrar JSON'a uygun sozluge cevirir."""
    return {
        "branding": {
            "title": config.branding.title,
            "subtitle": config.branding.subtitle,
            "logo_path": _serialize_path(config.base_dir, config.branding.logo_path),
        },
        "user_types": list(config.user_types),
        "companies": {
            name: {
                "prefix": values.prefix,
                "password": values.password,
            }
            for name, values in config.companies.items()
        },
        "profiles": {
            name: {
                "user_type": values.user_type,
                "company_name": values.company_name,
                "note": values.note,
                "options": dict(values.options),
            }
            for name, values in config.profiles.items()
        },
        "state_paths": {
            "domain_log_path": _serialize_path(config.base_dir, config.state_paths.domain_log_path),
            "eset_log_path": _serialize_path(config.base_dir, config.state_paths.eset_log_path),
            "user_cleanup_log_path": _serialize_path(config.base_dir, config.state_paths.user_cleanup_log_path),
        },
        "domain": {
            "name": config.domain.name,
            "username": config.domain.username,
            "password": config.domain.password,
        },
        "wifi_profiles": {
            name: {
                "ssid": values.ssid,
                "password": values.password,
            }
            for name, values in config.wifi_profiles.items()
        },
        "backup": {
            "network_path": config.backup.network_path,
            "network_user": config.backup.network_user,
            "network_password": config.backup.network_password,
            "folders": list(config.backup.folders),
        },
        "network_resources": {
            "credential_domain": config.network_resources.credential_domain,
            "required_wifi_ssid": config.network_resources.required_wifi_ssid,
            "printer_host": config.network_resources.printer_host,
            "printer_share": config.network_resources.printer_share,
            "file_server_host": config.network_resources.file_server_host,
            "file_server_share": config.network_resources.file_server_share,
            "file_server_shortcut_name": config.network_resources.file_server_shortcut_name,
        },
        "tools": {
            "local_admin_username": config.tools.local_admin_username,
            "local_admin_password": config.tools.local_admin_password,
            "anydesk_install_dir": config.tools.anydesk_install_dir,
            "anydesk_installer_path": config.tools.anydesk_installer_path,
            "eset_installer_path": config.tools.eset_installer_path,
            "hackbgrt_setup_path": config.tools.hackbgrt_setup_path,
        },
        "reporting": {
            "enabled": config.reporting.enabled,
            "output_dir": _serialize_path(config.base_dir, config.reporting.output_dir),
            "webhook_url": config.reporting.webhook_url,
            "webhook_token": config.reporting.webhook_token,
            "telegram_bot_token": config.reporting.telegram_bot_token,
            "telegram_chat_id": config.reporting.telegram_chat_id,
        },
        "desktop_automation": {
            "signature_source_dir": _serialize_optional_path(config.base_dir, config.desktop_automation.signature_source_dir),
            "signature_folder_name": config.desktop_automation.signature_folder_name,
            "outlook_classic_path": config.desktop_automation.outlook_classic_path,
            "outlook_email": config.desktop_automation.outlook_email,
            "outlook_password": config.desktop_automation.outlook_password,
            "wallpaper_source_path": _serialize_optional_path(config.base_dir, config.desktop_automation.wallpaper_source_path),
            "wallpaper_target_path": _serialize_optional_path(config.base_dir, config.desktop_automation.wallpaper_target_path),
            "lock_screen_source_path": _serialize_optional_path(config.base_dir, config.desktop_automation.lock_screen_source_path),
            "lock_screen_target_path": _serialize_optional_path(config.base_dir, config.desktop_automation.lock_screen_target_path),
            "wallpaper_lock_standard_users": config.desktop_automation.wallpaper_lock_standard_users,
        },
        "windows": {
            "activation_product_key": config.windows.activation_product_key,
            "restart_delay_seconds": config.windows.restart_delay_seconds,
            "update_uri": config.windows.update_uri,
        },
        "legacy_cleanup_user": config.legacy_cleanup_user,
    }


def save_app_config(config: AppConfig, path: Path | None = None) -> Path:
    """Config'i `app_config.local.json` veya verilen hedefe yazar."""
    if path is not None:
        target_path = path
    elif config.config_path.name == "app_config.local.json":
        target_path = config.config_path
    else:
        target_path = config.base_dir / "app_config.local.json"
    payload = app_config_to_dict(config)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target_path)
    finally:
        temp_path.unlink(missing_ok=True)
    if os.name == "nt":
        # NTFS üzerinde özel ayarı yalnızca SYSTEM ve yerel yöneticiler okuyup
        # değiştirebilir. FAT/exFAT USB'lerde ACL desteklenmeyebilir; bu yüzden
        # BitLocker ile şifrelenmiş NTFS operasyon USB'si kullanılmalıdır.
        subprocess.run(
            [
                "icacls.exe",
                str(target_path),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(F)",
                "*S-1-5-32-544:(F)",
            ],
            check=False,
            capture_output=True,
            creationflags=0x08000000,
        )
    return target_path


def validate_app_config(config: AppConfig) -> list[str]:
    """Return operator-facing configuration errors before settings are saved."""
    errors: list[str] = []
    if not config.companies:
        errors.append("En az bir sirket tanimlanmali.")
    for company_name, company in config.companies.items():
        if not company_name.strip():
            errors.append("Sirket adi bos olamaz.")
        if company.prefix and not re.fullmatch(r"[A-Za-z0-9-]{1,10}", company.prefix):
            errors.append(f"Sirket prefix'i gecersiz: {company_name}")

    any_rename_admin = any(profile.options.get("rename_admin") for profile in config.profiles.values())
    if any_rename_admin and (
        not config.tools.local_admin_username.strip()
        or not config.tools.local_admin_password
    ):
        errors.append("Lokaladm kullanici adi ve sifresi zorunlu.")
    if any(profile.user_type == "Domain" for profile in config.profiles.values()):
        if not config.domain.name.strip() or not config.domain.username.strip() or not config.domain.password:
            errors.append("Domain profili icin domain adi, kullanici ve sifre zorunlu.")

    for profile_name, profile in config.profiles.items():
        if profile.user_type not in config.user_types:
            errors.append(f"Profil kullanici tipi gecersiz: {profile_name}")
        if profile.company_name and profile.company_name not in config.companies:
            errors.append(f"Profilde tanimsiz sirket secili: {profile_name}")
        if profile.options.get("wifi_sync"):
            wifi = config.wifi_profiles.get("general")
            if not wifi or not wifi.ssid.strip() or not wifi.password:
                errors.append(f"Wi-Fi kullanan profil icin ag bilgisi eksik: {profile_name}")

    return sorted(set(errors))

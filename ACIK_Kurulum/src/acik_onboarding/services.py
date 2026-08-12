"""
Kurulumun is kurallarini ve Windows otomasyonunu tasiyan servis katmani.

Stajyer Notu:
- Bu dosya projenin "motoru"dur.
- UI tarafindan secilen veriler burada gercek Windows komutlarina, dosya
  islemlerine ve PowerShell akislaryna donusturulur.
- En kritik isler burada oldugu icin yeni ozellik eklerken once bu dosyadaki
  mevcut akis okunmalidir.
"""

from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import os
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse
from xml.sax.saxutils import escape as xml_escape

from .config import (
    AppConfig,
    CompanyProfile,
    WifiProfile,
    WorkflowProfile,
    repair_legacy_display_text,
)
from .payload_catalog import PAYLOAD_CATALOG
from .workflow import (
    STATE_SCHEMA_VERSION,
    TASK_PENDING,
    TASK_PERMANENT_FAILED,
    TASK_RETRYABLE_FAILED,
    TASK_RUNNING,
    TASK_SKIPPED,
    TASK_SUCCEEDED,
    USER_PHASE_TASKS,
    atomic_write_json,
    enabled_phase_tasks,
    make_task_map,
    mark_task,
    phase_status,
    read_json,
    unfinished_phase_tasks,
    utc_now,
    validate_state,
    workflow_status,
)


Logger = Callable[[str], None]
UiMessage = tuple[str, str, str]


# Portable test runs leave only a bounded, redacted failure record on the USB
# drive.  It is deliberately separate from the full ProgramData report, which
# can contain operator and device details that must not travel on removable
# media.
USB_DIAGNOSTIC_DIRECTORY = "ACIK_DIAGNOSTICS"
USB_DIAGNOSTIC_SCHEMA_VERSION = 1
USB_DIAGNOSTIC_MAX_BYTES = 512 * 1024

# A private deployment may supply an encrypted FortiClient export separately.
# Its password is deliberately never stored in this public source delivery.
# FCConfig receives it only from the process environment at run time.
FORTICLIENT_VPN_PROFILE_FILE = "MKR_FC_RA.sconf"
FORTICLIENT_VPN_CONNECTION_NAME = "MKR_FC_RA"
FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD = os.environ.get(
    "ACIK_FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD", ""
).strip()


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


@dataclass(slots=True)
class IdentityResult:
    """Ad soyaddan uretilen kullanici adi / PC adi / sifre paketidir."""
    username: str
    computer_name: str
    password: str


@dataclass(slots=True)
class OnboardingRequest:
    """UI'dan servise giden tekil kurulum istegi."""
    profile_name: str
    full_name: str
    company_name: str
    user_type: str
    username: str
    computer_name: str
    password: str
    options: dict[str, bool]
    run_id: str = ""


@dataclass(slots=True)
class PreflightCheckResult:
    """On kontroldeki tek bir kontrol satirini temsil eder."""
    name: str
    status: str
    detail: str


@dataclass(slots=True)
class StepResult:
    """Calisan kurulum adimlarinin rapor kaydidir."""
    name: str
    status: str
    detail: str
    started_at: str
    finished_at: str


class OnboardingService:
    """Kurulum adimlarini merkezi olarak yoneten servis."""
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def resolve_company(self, company_name: str) -> CompanyProfile:
        company_name = repair_legacy_display_text(company_name)
        if company_name not in self.config.companies:
            raise ValueError("Lutfen bir sirket secin.")
        return self.config.companies[company_name]

    def turkish_to_ascii(self, text: str) -> str:
        if any(marker in text for marker in ("\u00c3", "\u00c4", "\u00c5")):
            try:
                text = text.encode("cp1252").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        text = text.translate(
            str.maketrans(
                {
                    "\u0131": "i",
                    "\u0130": "I",
                    "\u00e7": "c",
                    "\u00c7": "C",
                    "\u015f": "s",
                    "\u015e": "S",
                    "\u011f": "g",
                    "\u011e": "G",
                    "\u00fc": "u",
                    "\u00dc": "U",
                    "\u00f6": "o",
                    "\u00d6": "O",
                }
            )
        )
        return "".join(
            character
            for character in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(character)
        )

    def create_username(self, text: str) -> str:
        words = [part for part in text.split() if part]
        if len(words) >= 2:
            candidate = f"{words[0].lower()}.{words[-1].lower()}"
        elif words:
            candidate = words[0].lower()
        else:
            return "default"
        candidate = re.sub(r"[^a-z0-9._-]", "", candidate)
        return candidate[:20].rstrip(". ") or "default"

    def create_special_username(self, text: str) -> str:
        words = [part for part in text.split() if part]
        if len(words) >= 2:
            candidate = f"{words[0][0]}{words[-1]}"
        elif words:
            candidate = words[0]
        else:
            return "default"
        return re.sub(r"[^A-Z0-9-]", "", candidate.upper()) or "DEFAULT"

    def generate_identity(self, full_name: str, company_name: str) -> IdentityResult:
        normalized_name = self.turkish_to_ascii(full_name.strip())
        if not normalized_name:
            raise ValueError("Ad soyad alani bos olamaz.")
        company = self.resolve_company(company_name)
        result = IdentityResult(
            username=self.create_username(normalized_name.lower()),
            computer_name=f"{company.prefix}{self.create_special_username(normalized_name.upper())}".upper()[:15].rstrip("-"),
            password=company.password,
        )
        self.validate_identity_values(
            full_name=full_name,
            user_type="Lokal",
            username=result.username,
            computer_name=result.computer_name,
            password=result.password,
            require_password=False,
        )
        return result

    def validate_identity_values(
        self,
        full_name: str,
        user_type: str,
        username: str,
        computer_name: str,
        password: str,
        require_password: bool = True,
    ) -> None:
        if not full_name.strip():
            raise ValueError("Ad soyad alani bos olamaz.")
        if user_type not in {"Lokal", "Domain"}:
            raise ValueError("Kullanici tipi Lokal veya Domain olmali.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", username):
            raise ValueError("Kullanici adi 1-20 karakter olmali ve yalnizca harf, rakam, nokta, alt cizgi veya tire icermeli.")
        reserved = {
            "administrator",
            "guest",
            "defaultaccount",
            "wdagutilityaccount",
            "system",
            "local service",
            "network service",
        }
        if username.lower() in reserved:
            raise ValueError(f"Kullanici adi Windows tarafindan ayrilmis: {username}")
        if username.endswith((".", " ")):
            raise ValueError("Kullanici adi nokta veya bosluk ile bitemez.")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,13}[A-Za-z0-9])?", computer_name):
            raise ValueError("PC adi 1-15 karakter olmali; yalnizca harf, rakam ve arada tire icermeli.")
        if require_password and not password:
            raise ValueError("Kullanici sifresi bos olamaz.")

    def validate_request(self, request: OnboardingRequest) -> None:
        self.resolve_company(request.company_name)
        self.validate_identity_values(
            request.full_name,
            request.user_type,
            request.username,
            request.computer_name,
            request.password,
        )
        cleanup_user = self.config.legacy_cleanup_user.strip()
        local_admin = self.config.tools.local_admin_username.strip()
        protected_names = {
            request.username.lower(),
            local_admin.lower(),
            "administrator",
        }
        if request.options.get("delete_x_user") and cleanup_user.lower() in protected_names:
            raise ValueError("Silinecek eski kullanici hedef, mevcut kullanici veya lokaladm ile ayni olamaz.")
        if request.options.get("delete_x_user") and not request.options.get("rename_admin"):
            raise ValueError("Eski kullanici temizligi icin Lokaladm hazirligi acik olmali.")
        if request.user_type == "Domain":
            domain = self.config.domain
            if not domain.name.strip() or not domain.username.strip() or not domain.password:
                raise ValueError("Domain ayarlari eksik.")

    def request_fingerprint(self, request: OnboardingRequest) -> str:
        payload = {
            "profile_name": request.profile_name,
            "full_name": request.full_name,
            "company_name": request.company_name,
            "user_type": request.user_type,
            "username": request.username,
            "computer_name": request.computer_name,
            "options": request.options,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def default_option_states(self) -> dict[str, bool]:
        return {
            "rename_admin": True,
            "ip_admin": False,
            "administrator": False,
            "delete_x_user": True,
            "wifi_sync": True,
            "main_file_server": False,
            "network_printer": False,
            "desktop_wallpaper": False,
            "lock_screen": False,
            "desktop_signature": False,
            "classic_outlook": False,
            "anydesk": False,
            "eset": True,
            "windows_update": False,
            "windows_activation": True,
            "restart": True,
            # HackBGRT EFI/NVRAM önyükleme kaydını değiştirir. Bu nedenle
            # standart kurulumda kapalı gelir; yalnızca bilinçli seçimle açılır.
            "hackbgrt": False,
        }

    def resolve_profile(self, profile_name: str) -> WorkflowProfile | None:
        if not profile_name:
            return None
        return self.config.profiles.get(profile_name)

    def build_profile_options(self, profile_name: str) -> dict[str, bool]:
        options = self.default_option_states()
        profile = self.resolve_profile(profile_name)
        if profile:
            options.update(profile.options)
        return options

    def is_admin_session(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def preflight_state_path(self) -> Path:
        return self.runtime_dir() / "last_preflight.json"

    def load_last_preflight(self) -> dict[str, object] | None:
        path = self.preflight_state_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def report_output_dir(self) -> Path:
        program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
        path = program_data / "AcikOnboarding" / "reports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load_report_history(self) -> list[dict[str, object]]:
        reports: list[dict[str, object]] = []
        report_dir = self.report_output_dir()
        for report_path in report_dir.glob("*.json"):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            payload["path"] = str(report_path)
            payload["_report_mtime"] = report_path.stat().st_mtime
            reports.append(payload)
        def report_time(report: dict[str, object]) -> tuple[float, str]:
            raw_value = str(report.get("run_started_at") or report.get("run_at") or "").strip()
            try:
                return (datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp(), raw_value)
            except ValueError:
                return (float(report.get("_report_mtime", 0.0)), raw_value)

        reports.sort(key=report_time, reverse=True)
        for report in reports:
            report.pop("_report_mtime", None)
        return reports

    def now_stamp(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def get_system_inventory(self, log: Logger) -> dict[str, object]:
        """Read device details without making WMI a single point of failure.

        Some managed Windows images restrict the current user's WMI/CIM access.
        The former all-CIM query therefore returned an empty report even though
        Windows could still expose the same basics through the registry and
        .NET.  This query uses those local sources first and only enriches the
        report with CIM data when it is available.
        """
        script = r"""
$ErrorActionPreference = 'Stop'
$collectionErrors = New-Object System.Collections.Generic.List[string]

function Get-RegistryText {
    param([string]$Path, [string]$Name)
    try {
        $value = (Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop).$Name
        if ($null -eq $value) { return '' }
        return (@($value) | Where-Object { $_ } | ForEach-Object { $_.ToString().Trim() }) -join ' | '
    } catch {
        return ''
    }
}

function Get-CimFirst {
    param([string]$ClassName, [string]$Filter = '')
    try {
        if ($Filter) {
            return Get-CimInstance -ClassName $ClassName -Filter $Filter -ErrorAction Stop | Select-Object -First 1
        }
        return Get-CimInstance -ClassName $ClassName -ErrorAction Stop | Select-Object -First 1
    } catch {
        return $null
    }
}

function Get-Text {
    param($Value)
    if ($null -eq $Value) { return '' }
    return $Value.ToString().Trim()
}

$biosRegistry = 'HKLM:\HARDWARE\DESCRIPTION\System\BIOS'
$osRegistry = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$manufacturer = Get-RegistryText $biosRegistry 'SystemManufacturer'
$model = Get-RegistryText $biosRegistry 'SystemProductName'
$serial = Get-RegistryText $biosRegistry 'SystemSerialNumber'
$processorName = Get-RegistryText 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0' 'ProcessorNameString'
$biosVersion = Get-RegistryText $biosRegistry 'BIOSVersion'
$biosDate = Get-RegistryText $biosRegistry 'BIOSReleaseDate'
$osCaption = Get-RegistryText $osRegistry 'ProductName'
$osVersion = Get-RegistryText $osRegistry 'DisplayVersion'
$osBuild = Get-RegistryText $osRegistry 'CurrentBuildNumber'

# CIM enriches the report when permitted, but is never required for it.
$cs = Get-CimFirst 'Win32_ComputerSystem'
$bios = Get-CimFirst 'Win32_BIOS'
$baseBoard = Get-CimFirst 'Win32_BaseBoard'
$systemProduct = Get-CimFirst 'Win32_ComputerSystemProduct'
$processor = Get-CimFirst 'Win32_Processor'
$os = Get-CimFirst 'Win32_OperatingSystem'
if (-not $manufacturer -and $cs) { $manufacturer = Get-Text $cs.Manufacturer }
if (-not $model -and $cs) { $model = Get-Text $cs.Model }
if (-not $serial -and $bios) { $serial = Get-Text $bios.SerialNumber }
if (-not $processorName -and $processor) { $processorName = Get-Text $processor.Name }
if (-not $biosVersion -and $bios) { $biosVersion = (@($bios.SMBIOSBIOSVersion, $bios.Version) | Where-Object { $_ } | Select-Object -Unique) -join ' | ' }
if (-not $osCaption -and $os) { $osCaption = Get-Text $os.Caption }
if (-not $osVersion -and $os) { $osVersion = Get-Text $os.Version }
if (-not $osBuild -and $os) { $osBuild = Get-Text $os.BuildNumber }

$totalMemoryBytes = [UInt64]0
try {
    Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
    $computerInfo = New-Object Microsoft.VisualBasic.Devices.ComputerInfo
    $totalMemoryBytes = [UInt64]$computerInfo.TotalPhysicalMemory
} catch {
    if ($cs -and $cs.TotalPhysicalMemory) { $totalMemoryBytes = [UInt64]$cs.TotalPhysicalMemory }
}
$totalMemoryGb = if ($totalMemoryBytes) { [math]::Round($totalMemoryBytes / 1GB, 2) } else { $null }

$memory = @()
try { $memory = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction Stop) } catch {}
$memorySpeeds = @($memory | ForEach-Object { $_.ConfiguredClockSpeed } | Where-Object { $_ } | Sort-Object -Unique)
$memorySummary = if ($memory.Count) {
    "$($memory.Count) modul" + $(if ($memorySpeeds.Count) { " | $($memorySpeeds -join '/') MHz" } else { '' })
} elseif ($totalMemoryGb) {
    'Windows bellek API ile okundu'
} else {
    ''
}

$systemDiskTotal = $null
$systemDiskFree = $null
try {
    $systemDrive = [System.IO.DriveInfo]::new('C')
    if ($systemDrive.IsReady) {
        $systemDiskTotal = [math]::Round($systemDrive.TotalSize / 1GB, 2)
        $systemDiskFree = [math]::Round($systemDrive.AvailableFreeSpace / 1GB, 2)
    }
} catch {}

$gpu = @()
try {
    $gpu = @(Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' -ErrorAction Stop |
        ForEach-Object { (Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue).DriverDesc } |
        Where-Object { $_ -and $_ -ne 'MS Idd Device' } | Sort-Object -Unique)
} catch {}
if (-not $gpu.Count) {
    try {
        $gpu = @(Get-CimInstance Win32_VideoController -ErrorAction Stop | ForEach-Object { $_.Name } |
            Where-Object { $_ -and $_ -ne 'MS Idd Device' } | Sort-Object -Unique)
    } catch {}
}

$physicalDisks = @()
try {
    $physicalDisks = @(Get-CimInstance Win32_DiskDrive -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            name = $_.DeviceID
            model = $_.Model
            interface = $_.InterfaceType
            size_gb = if ($_.Size) { [math]::Round($_.Size / 1GB, 2) } else { $null }
            size_display = if ($_.Size) { ('{0:N2} GB' -f ($_.Size / 1GB)) } else { 'Bilinmiyor' }
            serial_number = if ($_.SerialNumber) { $_.SerialNumber.Trim() } else { '' }
        }
    })
} catch {}
if (-not $physicalDisks.Count -and $null -ne $systemDiskTotal) {
    $physicalDisks = @([pscustomobject]@{
        name = 'C:'
        model = 'Sistem birimi'
        interface = 'Mantiksal disk'
        size_gb = $systemDiskTotal
        size_display = ('{0:N2} GB' -f $systemDiskTotal)
        serial_number = ''
    })
}

$tpm = $null
try {
    if (Get-Command Get-Tpm -ErrorAction SilentlyContinue) {
        $tpm = Get-Tpm -ErrorAction Stop
    }
} catch {}
$secureBoot = $null
try { $secureBoot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) } catch {}
$firmwareValue = Get-RegistryText 'HKLM:\SYSTEM\CurrentControlSet\Control' 'PEFirmwareType'
$firmwareType = if ($firmwareValue -eq '2') { 'UEFI' } elseif ($firmwareValue -eq '1') { 'BIOS' } else { 'Bilinmiyor' }
$processorCores = if ($processor -and $processor.NumberOfCores) { $processor.NumberOfCores } else { '' }
$processorLogical = if ($processor -and $processor.NumberOfLogicalProcessors) { $processor.NumberOfLogicalProcessors } else { [Environment]::ProcessorCount }
$processorTopology = if ($processorCores) { "$processorCores cekirdek / $processorLogical mantiksal" } elseif ($processorLogical) { "$processorLogical mantiksal islemci" } else { '' }
if ($os -and $os.OSArchitecture) {
    $architecture = Get-Text $os.OSArchitecture
} elseif ([Environment]::Is64BitOperatingSystem) {
    $architecture = '64-bit'
} else {
    $architecture = '32-bit'
}
$collectionStatus = if ($manufacturer -or $model -or $processorName -or $totalMemoryGb -or $osCaption) { 'complete' } else { 'partial' }
[pscustomobject]@{
    computer_name = $env:COMPUTERNAME
    serial_number = $serial
    manufacturer = $manufacturer
    model = $model
    system_uuid = if ($systemProduct) { Get-Text $systemProduct.UUID } else { '' }
    asset_tag = if ($systemProduct) { Get-Text $systemProduct.IdentifyingNumber } else { '' }
    processor_name = $processorName
    processor_cores = $processorCores
    processor_logical = $processorLogical
    processor_topology = $processorTopology
    total_memory_gb = $totalMemoryGb
    total_memory_display = if ($null -ne $totalMemoryGb) { ('{0:N2} GB' -f $totalMemoryGb) } else { '' }
    memory_module_count = $memory.Count
    memory_speeds_mhz = $memorySpeeds
    memory_summary = $memorySummary
    gpu_names = $gpu
    gpu_summary = $gpu -join ' | '
    motherboard_manufacturer = if ($baseBoard) { Get-Text $baseBoard.Manufacturer } else { $manufacturer }
    motherboard_model = if ($baseBoard) { Get-Text $baseBoard.Product } else { $model }
    motherboard_serial = if ($baseBoard) { Get-Text $baseBoard.SerialNumber } else { '' }
    motherboard_summary = (@($(if ($baseBoard) { Get-Text $baseBoard.Manufacturer } else { $manufacturer }), $(if ($baseBoard) { Get-Text $baseBoard.Product } else { $model })) | Where-Object { $_ }) -join ' '
    bios_version = $biosVersion
    bios_release_date = $biosDate
    bios_summary = (@($biosVersion, $biosDate) | Where-Object { $_ }) -join ' | '
    firmware_type = $firmwareType
    tpm_present = if ($tpm) { [bool]$tpm.TpmPresent } else { $false }
    tpm_ready = if ($tpm) { [bool]$tpm.TpmReady } else { $false }
    tpm_summary = if ($tpm -and $tpm.TpmPresent) { if ($tpm.TpmReady) { 'Mevcut ve hazir' } else { 'Mevcut, hazir degil' } } else { 'Bulunamadi' }
    secure_boot_enabled = $secureBoot
    secure_boot_summary = if ($null -eq $secureBoot) { 'Desteklenmiyor veya okunamadi' } elseif ($secureBoot) { 'Acik' } else { 'Kapali' }
    os_caption = $osCaption
    os_version = $osVersion
    os_build = $osBuild
    os_architecture = $architecture
    os_summary = (@($osCaption, $(if ($osVersion) { "Surum $osVersion" }), $(if ($osBuild) { "Build $osBuild" }), $architecture) | Where-Object { $_ }) -join ' | '
    total_disk_gb = $systemDiskTotal
    free_disk_gb = $systemDiskFree
    system_disk_summary = if ($null -ne $systemDiskTotal) { ('{0:N2} GB bos / {1:N2} GB toplam' -f $systemDiskFree, $systemDiskTotal) } else { '' }
    physical_disks = $physicalDisks
    collection_status = $collectionStatus
    collection_errors = @($collectionErrors)
} | ConvertTo-Json -Depth 5 -Compress
"""
        completed = self.run_powershell(script, log, check=False)
        raw = completed.stdout.strip().splitlines()
        empty: dict[str, object] = {
            "computer_name": "",
            "serial_number": "",
            "manufacturer": "",
            "model": "",
            "system_uuid": "",
            "asset_tag": "",
            "processor_name": "",
            "processor_topology": "",
            "total_memory_gb": None,
            "total_memory_display": "",
            "memory_summary": "",
            "gpu_summary": "",
            "motherboard_summary": "",
            "motherboard_serial": "",
            "bios_summary": "",
            "firmware_type": "",
            "tpm_summary": "",
            "secure_boot_summary": "",
            "os_caption": "",
            "os_version": "",
            "os_summary": "",
            "system_disk_summary": "",
            "free_disk_gb": None,
            "physical_disks": [],
            "collection_status": "partial",
            "collection_errors": [],
        }
        if not raw:
            log("Envanter sorgusu cikti uretmedi.")
            return empty
        for line in reversed(raw):
            try:
                data = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            empty.update(data)
            return empty
        log("Envanter JSON ciktisi okunamadi.")
        return empty

    def internet_available(self) -> bool:
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=3).close()
            return True
        except OSError:
            return False

    def can_resolve_host(self, host: str) -> bool:
        if not host.strip():
            return False
        try:
            socket.gethostbyname(host)
            return True
        except OSError:
            return False

    def tcp_reachable(self, host: str, port: int, timeout: float = 2.5) -> bool:
        if not host.strip():
            return False
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            return False

    def verify_payload_integrity(self, path: Path) -> None:
        try:
            relative = path.resolve().relative_to(self.config.base_dir.resolve()).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"Payload uygulama kokunun disinda: {path}") from exc
        expected = PAYLOAD_CATALOG.get(relative)
        if not isinstance(expected, dict):
            raise RuntimeError(f"Payload gomulu katalogda kayitli degil: {relative}")
        if path.stat().st_size != int(expected.get("size", -1)):
            raise RuntimeError(f"Payload boyutu degismis: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().casefold() != str(expected.get("sha256", "")).casefold():
            raise RuntimeError(f"Payload SHA-256 dogrulamasi basarisiz: {relative}")

    def verify_external_payload_integrity(self, path: Path) -> None:
        """Verify a USB utility by matching its basename to the embedded catalog."""
        candidates = [
            entry
            for relative, entry in PAYLOAD_CATALOG.items()
            if Path(relative).name.casefold() == path.name.casefold()
        ]
        if not candidates:
            raise RuntimeError(f"Harici payload gomulu katalogda kayitli degil: {path.name}")
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_hash = digest.hexdigest().casefold()
        if not any(
            size == int(candidate.get("size", -1))
            and actual_hash == str(candidate.get("sha256", "")).casefold()
            for candidate in candidates
        ):
            raise RuntimeError(f"Harici payload butunluk dogrulamasi basarisiz: {path.name}")

    def verify_known_payload_integrity(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.config.base_dir.resolve())
        except ValueError:
            self.verify_external_payload_integrity(path)
        else:
            self.verify_payload_integrity(path)

    def validate_payloads(
        self,
        selected_options: dict[str, bool],
        *,
        validate_wallpaper: bool = False,
    ) -> list[PreflightCheckResult]:
        checks: list[PreflightCheckResult] = []
        if selected_options.get("eset"):
            try:
                installer = self._resolve_tool_path(self.config.tools.eset_installer_path)
                if installer.exists():
                    self.verify_payload_integrity(installer)
                checks.append(
                    PreflightCheckResult(
                        "ESET dosyasi",
                        "ok" if installer.exists() else "error",
                        str(installer) if installer.exists() else f"ESET dosyasi bulunamadi: {installer}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(PreflightCheckResult("ESET dosyasi", "error", str(exc)))
        if selected_options.get("anydesk"):
            installer = self._resolve_optional_tool_path(self.config.tools.anydesk_installer_path)
            if installer is None:
                checks.append(
                    PreflightCheckResult(
                        "AnyDesk payload",
                        "warning",
                        "Yerel AnyDesk paketi tanimli degil. Gerekirse internet indirimi kullanilacak.",
                    )
                )
            else:
                try:
                    if installer.exists():
                        self.verify_payload_integrity(installer)
                    checks.append(
                        PreflightCheckResult(
                            "AnyDesk payload",
                            "ok" if installer.exists() else "warning",
                            str(installer) if installer.exists() else f"Yerel AnyDesk paketi bulunamadi: {installer}. Internet indirimi denenecek.",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    checks.append(PreflightCheckResult("AnyDesk payload", "error", str(exc)))
        if selected_options.get("hackbgrt"):
            try:
                path = self.resolve_hackbgrt_source_dir()
                self.validate_hackbgrt_dir(path)
                for required_name in ("setup.exe", "config.txt", "splash.bmp"):
                    self.verify_payload_integrity(path / required_name)
                checks.append(PreflightCheckResult("HackBGRT paketi", "ok", str(path)))
            except Exception as exc:  # noqa: BLE001
                checks.append(PreflightCheckResult("HackBGRT paketi", "error", str(exc)))
        if validate_wallpaper and selected_options.get("desktop_wallpaper"):
            try:
                source = self.resolve_wallpaper_source()
                target = self.resolve_wallpaper_target()
            except RuntimeError as exc:
                source = Path()
                target = Path()
                wallpaper_error = str(exc)
            else:
                wallpaper_error = ""
            checks.append(
                PreflightCheckResult(
                    "Duvar kağıdı kaynağı",
                    "ok" if source and str(source) != "." and source.exists() else "error",
                    str(source) if source and str(source) != "." else (wallpaper_error or "Duvar kağıdı kaynağı ayarlanmamış."),
                )
            )
            checks.append(
                PreflightCheckResult(
                    "Duvar kağıdı hedefi",
                    "ok" if target and str(target) != "." else "error",
                    str(target) if target and str(target) != "." else "Duvar kağıdı hedef yolu ayarlanmamış.",
                )
            )
        if selected_options.get("lock_screen"):
            try:
                lock_screen_source = self.resolve_lock_screen_source()
                checks.append(
                    PreflightCheckResult(
                        "Kilit ekranı kaynağı",
                        "ok",
                        str(lock_screen_source),
                    )
                )
            except RuntimeError as exc:
                checks.append(PreflightCheckResult("Kilit ekranı kaynağı", "error", str(exc)))
        return checks

    def run_preflight(self, request: OnboardingRequest, log: Logger) -> list[UiMessage]:
        """Kurulumdan once kritik riskleri tarar ve sonucu runtime altina yazar.

        Stajyer Notu:
        - Burasi "kurulumu calistirmadan once saglik kontrolu" bolumudur.
        - Hata varsa onboarding'i tamamen durdurmaz; sadece operatoru uyarir.
        - UI tarafindaki on kontrol tablosu bu fonksiyonun urettiği veriyi okur.
        """
        self.validate_request(request)
        checks: list[PreflightCheckResult] = []
        inventory = self.get_system_inventory(log)
        is_admin = self.is_admin_session()
        internet_ok = self.internet_available()

        checks.append(
            PreflightCheckResult(
                "Yonetici yetkisi",
                "ok" if is_admin else "error",
                "Yonetici yetkisi algilandi." if is_admin else "Uygulama yonetici olarak calismali.",
            )
        )
        needs_wifi_profile = bool(
            request.options.get("wifi_sync")
            or request.options.get("main_file_server")
            or request.options.get("network_printer")
        )
        if needs_wifi_profile:
            checks.append(
                PreflightCheckResult(
                    "Wi-Fi profili",
                    "ok" if self.config.wifi_profiles.get("general") and self.config.wifi_profiles["general"].ssid else "error",
                    self.config.wifi_profiles.get("general").ssid if self.config.wifi_profiles.get("general") else "Genel Wi-Fi profili eksik.",
                )
            )
        checks.append(
            PreflightCheckResult(
                "Internet",
                "ok" if internet_ok else "warning",
                "Internet erisimi var." if internet_ok else "Internet baglantisi dogrulanamadi.",
            )
        )
        free_disk = inventory.get("free_disk_gb")
        disk_ok = isinstance(free_disk, (int, float)) and free_disk >= 10
        checks.append(
            PreflightCheckResult(
                "Disk alani",
                "ok" if disk_ok else "warning",
                f"C surucusunde bos alan: {free_disk} GB" if free_disk is not None else "Disk bilgisi okunamadi.",
            )
        )

        if request.user_type == "Domain":
            domain_host = self.config.domain.name.strip()
            domain_ok = bool(domain_host) and (self.can_resolve_host(domain_host) or self.tcp_reachable(domain_host, 445))
            checks.append(
                PreflightCheckResult(
                    "Domain erisimi",
                    "ok" if domain_ok else "warning",
                    domain_host if domain_ok else f"Domain erisimi dogrulanamadi: {domain_host or 'bos'}",
                )
            )

        if request.options.get("main_file_server"):
            host = self.config.network_resources.file_server_host.strip()
            checks.append(
                PreflightCheckResult(
                    "File Server ayari",
                    "ok" if host and self.config.network_resources.file_server_share.strip() else "error",
                    self.build_unc_path(host, self.config.network_resources.file_server_share.strip()) if host else "File Server host/share eksik.",
                )
            )
        if request.options.get("network_printer"):
            host = self.config.network_resources.printer_host.strip()
            checks.append(
                PreflightCheckResult(
                    "Yazici ayari",
                    "ok" if host and self.config.network_resources.printer_share.strip() else "error",
                    self.build_unc_path(host, self.config.network_resources.printer_share.strip()) if host else "Yazici host/share eksik.",
                )
            )

        if request.options.get("desktop_signature"):
            signature_root = self.config.desktop_automation.signature_source_dir
            checks.append(
                PreflightCheckResult(
                    "Imza dosyalari",
                    "ok" if signature_root and str(signature_root) != "." and signature_root.exists() else "warning",
                    str(signature_root) if signature_root and str(signature_root) != "." else "Imza klasoru ayarlanmamis.",
                )
            )

        if request.options.get("classic_outlook"):
            desktop = self.config.desktop_automation
            outlook_ready = bool(desktop.outlook_classic_path.strip())
            checks.append(
                PreflightCheckResult(
                    "Outlook hazirligi",
                    "ok" if outlook_ready else "warning",
                    desktop.outlook_classic_path if outlook_ready else "Outlook Classic yolu eksik.",
                )
            )

        if (
            request.user_type == "Lokal"
            and request.options.get("desktop_wallpaper")
            and not request.options.get("administrator")
        ):
            try:
                source = self.resolve_wallpaper_source()
                target = self.resolve_wallpaper_target()
            except RuntimeError as exc:
                source = Path()
                target = Path()
                wallpaper_error = str(exc)
            else:
                wallpaper_error = ""
            checks.append(
                PreflightCheckResult(
                    "Sabit arka plan",
                    "ok" if source and target and str(source) != "." and str(target) != "." and source.exists() else "error",
                    f"{source} -> {target}" if source and target and str(source) != "." and str(target) != "." else (wallpaper_error or "Duvar kağıdı kaynağı veya hedefi eksik."),
                )
            )

        if request.options.get("hackbgrt"):
            try:
                self.ensure_hackbgrt_prerequisites(log)
                checks.append(PreflightCheckResult("HackBGRT on kosul", "ok", "UEFI ve Secure Boot durumu uygun."))
            except Exception as exc:  # noqa: BLE001
                # HackBGRT is explicitly optional and modifies EFI boot data.
                # Unsupported firmware must never hold back unrelated critical
                # onboarding work such as the verified SYSTEM X cleanup.
                request.options["hackbgrt"] = False
                checks.append(
                    PreflightCheckResult(
                        "HackBGRT on kosul",
                        "warning",
                        f"HackBGRT atlandi: {exc}",
                    )
                )
                log(f"HackBGRT secimi desteklenmeyen cihazda kapatildi: {exc}")

        checks.extend(self.validate_payloads(request.options))

        for check in checks:
            status_map = {"ok": "OK", "warning": "UYARI", "error": "HATA"}
            log(f"On kontrol | {status_map.get(check.status, check.status.upper())} | {check.name} | {check.detail}")

        payload = {
            "run_at": self.now_stamp(),
            "request_fingerprint": self.request_fingerprint(request),
            "request": {
                "profile_name": request.profile_name,
                "company_name": request.company_name,
                "user_type": request.user_type,
                "username": request.username,
                "computer_name": request.computer_name,
                "options": request.options,
            },
            "inventory": inventory,
            "checks": [asdict(check) for check in checks],
        }
        self.preflight_state_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"On kontrol raporu yazildi: {self.preflight_state_path()}")

        has_errors = any(check.status == "error" for check in checks)
        has_warnings = any(check.status == "warning" for check in checks)
        if has_errors:
            failed_checks = " | ".join(
                f"{check.name}: {check.detail}"
                for check in checks
                if check.status == "error"
            )
            self.export_task_failure_to_usb("Sistem on kontrolu", failed_checks, log)
            return [("On Kontrol", "En az bir kritik kontrol basarisiz oldu. Log ekranini incele.", "error")]
        if has_warnings:
            return [("On Kontrol", "Kontroller tamamlandi. Uyari olan maddeleri logdan inceleyebilirsin.", "warning")]
        return [("On Kontrol", "Tum kontroller basariyla tamamlandi.", "info")]

    def preflight_errors_for(self, request: OnboardingRequest) -> list[str]:
        payload = self.load_last_preflight()
        if not payload:
            return ["On kontrol sonucu bulunamadi."]
        if payload.get("request_fingerprint") != self.request_fingerprint(request):
            return ["Form degisti; on kontrol yeniden calistirilmali."]
        checks = payload.get("checks", [])
        if not isinstance(checks, list):
            return ["On kontrol sonucu okunamadi."]
        return [
            f"{check.get('name', 'Kontrol')}: {check.get('detail', '')}"
            for check in checks
            if isinstance(check, dict) and check.get("status") == "error"
        ]

    def create_step_result(self, name: str, status: str, detail: str, started_at: datetime) -> StepResult:
        return StepResult(
            name=name,
            status=status,
            detail=detail,
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=self.now_stamp(),
        )

    def write_run_report(self, payload: dict[str, object], log: Logger) -> Path:
        report_dir = self.report_output_dir()
        run_id = str(payload.get("run_id", "")).strip()
        if run_id:
            report_path = report_dir / f"{run_id}.json"
        else:
            safe_name = str(payload.get("computer_name", "report")).replace(" ", "_")
            report_path = report_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}.json"
        atomic_write_json(report_path, payload)
        log(f"Kurulum raporu yazildi: {report_path}")
        return report_path

    @staticmethod
    def _redact_diagnostic_text(
        value: object,
        *,
        limit: int = 1200,
        extra_secrets: tuple[str, ...] = (),
    ) -> str:
        """Keep portable diagnostics useful without copying credentials.

        Installer output sometimes echoes command-line fragments.  Redact the
        common secret labels before a diagnostic record leaves the machine and
        bound every field so a broken installer cannot fill the USB drive.

        ``extra_secrets`` additionally blanks out specific known plaintext
        secret values verbatim (e.g. a password just embedded into a
        PowerShell script via ``ConvertTo-SecureString '<value>' ...``),
        regardless of whether the surrounding text has a "password:"-style
        label PowerShell could echo back on a parse/runtime error.
        """
        text = str(value or "").replace("\x00", " ").strip()
        for secret in extra_secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
        text = re.sub(
            r"(?i)\b(password|parola|token|secret|api[_ -]?key|anahtar)\b\s*([:=])\s*[^\s,;]+",
            r"\1\2<redacted>",
            text,
        )
        return text[:limit]

    @staticmethod
    def _safe_diagnostic_run_id(value: object) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:64]

    def diagnostic_drive_roots(self) -> list[Path]:
        """Return removable USB drive roots, without probing fixed disks."""
        if sys.platform != "win32":
            return []
        try:
            kernel32 = ctypes.windll.kernel32
            drive_mask = int(kernel32.GetLogicalDrives())
            roots: list[Path] = []
            # DRIVE_REMOVABLE.  Network and fixed drives must never be used for
            # portable diagnostics.
            for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
                if not drive_mask & (1 << index):
                    continue
                root = Path(f"{letter}:/")
                if int(kernel32.GetDriveTypeW(str(root))) == 2:
                    roots.append(root)
            return roots
        except Exception:
            return []

    def portable_diagnostic_root(self) -> Path | None:
        """Return the test USB root without ever choosing an arbitrary drive.

        A portable EXE uses its own removable drive.  When the operator runs
        the UI locally but installs payloads from a USB, the one removable
        drive explicitly marked with ``1.UTIL_KURULUM`` is also accepted.
        Multiple candidate USB drives are deliberately ambiguous and result in
        no write rather than a potentially wrong write.
        """
        try:
            package_path = self.config.base_dir.resolve()
        except OSError:
            return None
        roots = self.diagnostic_drive_roots()
        for root in roots:
            try:
                package_path.relative_to(root.resolve())
            except ValueError:
                continue
            return root
        dedicated_roots = [
            root
            for root in roots
            if (root / "1.UTIL_KURULUM").is_dir()
            or (root / USB_DIAGNOSTIC_DIRECTORY).is_dir()
        ]
        if len(dedicated_roots) == 1:
            return dedicated_roots[0]
        return None

    @staticmethod
    def _diagnostic_directory_for_root(root: Path) -> Path:
        root_resolved = root.resolve()
        target = (root_resolved / USB_DIAGNOSTIC_DIRECTORY).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError as exc:
            raise RuntimeError("USB hata kaydi hedefi guvenli degil.") from exc
        return target

    def write_usb_diagnostic(self, payload: dict[str, object], log: Logger) -> Path | None:
        """Write one sanitized failure bundle beside a USB-launched package.

        The method is intentionally best-effort: setup failure reporting must
        never hide or replace the original setup error.
        """
        root = self.portable_diagnostic_root()
        if root is None:
            return None
        try:
            output_dir = self._diagnostic_directory_for_root(root)
            output_dir.mkdir(parents=True, exist_ok=True)
            run_id = self._safe_diagnostic_run_id(payload.get("run_id")) or "failure"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = output_dir / f"{timestamp}_{run_id}.json"
            atomic_write_json(path, payload)
            log(f"USB hata kaydi yazildi: {path}")
            return path
        except (OSError, RuntimeError) as exc:
            log(f"USB hata kaydi yazilamadi: {exc}")
            return None

    def export_run_failure_to_usb(self, report: dict[str, object], log: Logger) -> Path | None:
        """Create the removable-media-safe view of a failed/partial run."""
        status = str(report.get("status", "")).strip().lower()
        if status not in {"failed", "partial"}:
            return None

        steps: list[dict[str, str]] = []
        raw_steps = report.get("steps", [])
        if isinstance(raw_steps, list):
            for raw_step in raw_steps[:80]:
                if not isinstance(raw_step, dict):
                    continue
                step_status = str(raw_step.get("status", "")).strip().lower()
                if step_status not in {"error", "failed"}:
                    continue
                steps.append(
                    {
                        "name": self._redact_diagnostic_text(raw_step.get("name"), limit=120),
                        "status": step_status,
                        "detail": self._redact_diagnostic_text(raw_step.get("detail")),
                    }
                )

        payload: dict[str, object] = {
            "schema_version": USB_DIAGNOSTIC_SCHEMA_VERSION,
            "kind": "onboarding_failure",
            "created_at": self.now_stamp(),
            "run_id": self._safe_diagnostic_run_id(report.get("run_id")),
            "status": status,
            "error": self._redact_diagnostic_text(report.get("error")),
            "failed_steps": steps,
        }
        return self.write_usb_diagnostic(payload, log)

    def export_task_failure_to_usb(
        self,
        task_name: str,
        error: str,
        log: Logger,
        *,
        run_id: str = "",
    ) -> Path | None:
        """Record failures outside the main onboarding flow (for example USB tools)."""
        payload: dict[str, object] = {
            "schema_version": USB_DIAGNOSTIC_SCHEMA_VERSION,
            "kind": "task_failure",
            "created_at": self.now_stamp(),
            "run_id": self._safe_diagnostic_run_id(run_id),
            "status": "failed",
            "task": self._redact_diagnostic_text(task_name, limit=160),
            "error": self._redact_diagnostic_text(error),
            "failed_steps": [],
        }
        return self.write_usb_diagnostic(payload, log)

    def load_usb_diagnostics(self) -> list[dict[str, object]]:
        """Read validated, bounded diagnostic JSON from connected USB drives."""
        records: list[dict[str, object]] = []
        for root in self.diagnostic_drive_roots():
            try:
                diagnostic_dir = self._diagnostic_directory_for_root(root)
                if not diagnostic_dir.is_dir():
                    continue
                for path in sorted(diagnostic_dir.glob("*.json"), reverse=True):
                    try:
                        if path.is_symlink() or path.stat().st_size > USB_DIAGNOSTIC_MAX_BYTES:
                            continue
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("schema_version") != USB_DIAGNOSTIC_SCHEMA_VERSION:
                        continue
                    if payload.get("kind") not in {"onboarding_failure", "task_failure"}:
                        continue
                    payload["path"] = str(path)
                    records.append(payload)
            except OSError:
                continue
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records

    def format_usb_diagnostic_report(self, records: list[dict[str, object]]) -> str:
        """Build a compact operator-facing report for the Program Kurulumu tab."""
        if not records:
            return "Bagli USB bellekte ACIK_DIAGNOSTICS hata kaydi bulunamadi."

        lines = [f"{len(records)} USB hata kaydi bulundu."]
        for record in records[:12]:
            created_at = self._redact_diagnostic_text(record.get("created_at"), limit=40)
            task = self._redact_diagnostic_text(record.get("task"), limit=160)
            error = self._redact_diagnostic_text(record.get("error"), limit=500)
            heading = task or "Kurulum akisi"
            lines.append(f"\n[{created_at}] {heading}")
            if error:
                lines.append(f"Hata: {error}")
            raw_steps = record.get("failed_steps", [])
            if isinstance(raw_steps, list):
                for step in raw_steps[:4]:
                    if isinstance(step, dict):
                        name = self._redact_diagnostic_text(step.get("name"), limit=120)
                        detail = self._redact_diagnostic_text(step.get("detail"), limit=300)
                        lines.append(f"- {name}: {detail}")
        if len(records) > 12:
            lines.append(f"\nEk {len(records) - 12} kayit listelenmedi.")
        return "\n".join(lines)

    def send_report_webhook(self, payload: dict[str, object], log: Logger) -> None:
        if not (self.config.reporting.enabled and self.config.reporting.webhook_url.strip()):
            return
        webhook_url = self.config.reporting.webhook_url.strip()
        parsed = urlparse(webhook_url)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise RuntimeError("Rapor webhook adresi gecerli bir HTTPS URL olmali.")
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.reporting.webhook_token.strip()}",
            } if self.config.reporting.webhook_token.strip() else {
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            log(f"Webhook raporu gonderildi. HTTP {response.status}")

    def send_report_telegram(self, payload: dict[str, object], log: Logger) -> None:
        reporting = self.config.reporting
        if not (reporting.enabled and reporting.telegram_bot_token.strip() and reporting.telegram_chat_id.strip()):
            return
        text = (
            f"Kurulum Tamamlandi\n"
            f"Cihaz: {payload.get('computer_name', '')}\n"
            f"Kullanici: {payload.get('username', '')}\n"
            f"Sirket: {payload.get('company_name', '')}\n"
            f"Seri No: {payload.get('inventory', {}).get('serial_number', '') if isinstance(payload.get('inventory'), dict) else ''}\n"
            f"Durum: {payload.get('status', '')}"
        )
        body = urlencode({"chat_id": reporting.telegram_chat_id.strip(), "text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{reporting.telegram_bot_token.strip()}/sendMessage",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            log(f"Telegram bildirimi gonderildi. HTTP {response.status}")

    def dispatch_run_report(self, payload: dict[str, object], log: Logger) -> None:
        if not self.config.reporting.enabled:
            return
        try:
            self.send_report_webhook(payload, log)
        except Exception as exc:  # noqa: BLE001
            log(f"Webhook raporu gonderilemedi: {exc}")
        try:
            self.send_report_telegram(payload, log)
        except Exception as exc:  # noqa: BLE001
            log(f"Telegram bildirimi gonderilemedi: {exc}")

    def has_custom_action(self, action_name: str) -> bool:
        if action_name == "hackbgrt":
            return bool(self.config.tools.hackbgrt_setup_path.strip())
        if action_name == "main_file_server":
            resource = self.config.network_resources
            return bool(resource.file_server_host.strip() and resource.file_server_share.strip())
        if action_name == "network_printer":
            resource = self.config.network_resources
            return bool(resource.printer_host.strip() and resource.printer_share.strip())
        if action_name == "desktop_wallpaper":
            try:
                return self.resolve_wallpaper_source().exists()
            except RuntimeError:
                return False
        if action_name == "desktop_signature":
            source_dir = self.config.desktop_automation.signature_source_dir
            return bool(source_dir and str(source_dir) != "." and source_dir.exists())
        if action_name == "classic_outlook":
            desktop = self.config.desktop_automation
            return bool(desktop.outlook_classic_path.strip())
        return True

    def _run(
        self,
        args: list[str],
        log: Logger,
        check: bool = True,
        cwd: str | None = None,
        timeout_seconds: int = 900,
    ) -> subprocess.CompletedProcess[str]:
        is_installer = False
        if args and str(args[0]).lower().endswith(".exe"):
            basename = Path(args[0]).name.lower()
            if basename not in ["net.exe", "net1.exe", "schtasks.exe", "netsh.exe", "cmd.exe", "powershell.exe"]:
                is_installer = True

        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000

        try:
            completed = subprocess.run(
                args,
                check=False,
                stdout=subprocess.DEVNULL if is_installer else subprocess.PIPE,
                stderr=subprocess.DEVNULL if is_installer else subprocess.PIPE,
                text=True if not is_installer else None,
                encoding="utf-8" if not is_installer else None,
                errors="replace" if not is_installer else None,
                cwd=cwd,
                creationflags=creationflags,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Komut {timeout_seconds} saniyede tamamlanmadi: {Path(args[0]).name}"
            ) from exc
        if not is_installer:
            # Same redaction as run_powershell(): net.exe/schtasks.exe/etc. output
            # is logged verbatim otherwise, and a future caller piping a secret
            # through one of these allowlisted executables must not silently
            # bypass the app's only redaction layer.
            if completed.stdout and completed.stdout.strip():
                log(self._redact_diagnostic_text(completed.stdout.strip(), limit=1800))
            if completed.stderr and completed.stderr.strip():
                log(self._redact_diagnostic_text(completed.stderr.strip(), limit=1800))
        if check and completed.returncode != 0:
            raise RuntimeError(f"Komut basarisiz oldu (kod {completed.returncode}).")
        return completed

    def _run_quiet(
        self,
        args: list[str],
        timeout_seconds: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000
        try:
            return subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=args,
                returncode=124,
                stdout="",
                stderr=f"Komut {timeout_seconds} saniyede zaman asimina ugradi.",
            )

    def ps_escape(self, value: str) -> str:
        return value.replace("'", "''")

    def protect_secret(self, value: str) -> str:
        """Protect a secret for this Windows machine using DPAPI."""
        if not value:
            return ""
        raw = value.encode("utf-8")
        if sys.platform != "win32":
            return "test:" + base64.b64encode(raw).decode("ascii")

        input_buffer = ctypes.create_string_buffer(raw)
        input_blob = _DataBlob(
            len(raw),
            ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DataBlob()
        flags = 0x1 | 0x4  # UI forbidden | local machine
        success = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "ACIK-Onboarding",
            None,
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
        if not success:
            raise ctypes.WinError()
        try:
            protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return "dpapi:" + base64.b64encode(protected).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(output_blob.pbData)

    def unprotect_secret(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("test:") and sys.platform != "win32":
            return base64.b64decode(value.removeprefix("test:")).decode("utf-8")
        if not value.startswith("dpapi:"):
            raise RuntimeError("Korunan kimlik bilgisi formati gecersiz.")

        protected = base64.b64decode(value.removeprefix("dpapi:"))
        input_buffer = ctypes.create_string_buffer(protected)
        input_blob = _DataBlob(
            len(protected),
            ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DataBlob()
        success = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
        if not success:
            raise ctypes.WinError()
        try:
            raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return raw.decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(output_blob.pbData)

    def _target_principal(self, username: str, user_type: str) -> str:
        if user_type == "Domain":
            domain_name = self.config.domain.name.split(".", 1)[0].strip()
            return f"{domain_name}\\{username}" if domain_name else username
        # A local account must still resolve after the initial restart applies
        # a computer rename. The local-machine shorthand stays valid across
        # that rename and is also accepted by Task Scheduler.
        return f".\\{username}"

    def resolve_account_sid(self, principal: str, log: Logger | None = None) -> str:
        if sys.platform != "win32" or not principal.strip():
            return ""
        # ``NTAccount.Translate`` does not reliably understand the ``.\\user``
        # shorthand in an elevated process.  That used to leave local accounts
        # without a SID and Task Scheduler then rejected ``UserId=.\\user``.
        # Prefer LocalAccounts, but some OEM Windows images expose that module
        # incompletely.  Win32_UserAccount is the independent fallback, so a
        # valid newly created local account is never handed to Task Scheduler
        # as the invalid ``.\\user`` shorthand.
        escaped_principal = self.ps_escape(principal)
        completed = self.run_powershell(
            "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    f"$principal = '{escaped_principal}'",
                    "if ($principal -match '^\\.(?:\\\\|/)(.+)$') {",
                    "    $localUsername = $Matches[1]",
                    "    Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction SilentlyContinue",
                    "    $localUser = Get-LocalUser -Name $localUsername -ErrorAction SilentlyContinue",
                    "    if (-not $localUser) {",
                    "        try {",
                    "        $escapedLocalUsername = $localUsername.Replace(\"'\", \"''\")",
                    "        $localUser = Get-CimInstance Win32_UserAccount -Filter \"LocalAccount=True AND Name='$escapedLocalUsername'\" -ErrorAction Stop | Select-Object -First 1",
                    "        } catch { $localUser = $null }",
                    "    }",
                    "    $localSid = if ($localUser) { [string]$localUser.SID } else { '' }",
                    "    if (-not $localSid) {",
                    "        try {",
                    "            $localAccount = [System.Security.Principal.NTAccount]::new($env:COMPUTERNAME, $localUsername)",
                    "            $localSid = $localAccount.Translate([System.Security.Principal.SecurityIdentifier]).Value",
                    "        } catch {}",
                    "    }",
                    "    if (-not $localSid) { throw \"Yerel kullanici SID'i bulunamadi.\" }",
                    "    $localSid",
                    "} else {",
                    "    $account = [System.Security.Principal.NTAccount]::new($principal)",
                    "    $account.Translate([System.Security.Principal.SecurityIdentifier]).Value",
                    "}",
                ]
            ),
            log or (lambda _message: None),
            check=False,
        )
        if completed.returncode != 0:
            return ""
        rows = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return rows[-1] if rows and re.fullmatch(r"S-\d(?:-\d+)+", rows[-1]) else ""

    def close_failed_workflow_for_retry(
        self,
        state: dict[str, object],
        log: Logger,
    ) -> bool:
        """Retire a failed run so it cannot block a fresh operator retry.

        A failed initial flow can leave its second-phase state and Scheduled
        Tasks behind. The report is the proof that the run is no longer valid.
        Preserve it for audit, mark it closed, remove related artifacts, and
        let the new setup create a clean run. Pending and partial workflows
        are deliberately not auto-closed.
        """
        run_id = str(state.get("run_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            return False
        report_path = self.report_output_dir() / f"{run_id}.json"
        if not report_path.is_file():
            return False
        try:
            report = read_json(report_path)
        except Exception as exc:  # noqa: BLE001 - keep an unreadable run blocked
            log(f"Eski failed raporu okunamadi; yeni kurulum bloke tutuldu: {exc}")
            return False
        if str(report.get("status", "")).strip().casefold() != "failed":
            return False

        self.cancel_active_workflow(log)
        if self.post_login_state_path().exists():
            log("Eski failed akisin durum dosyasi temizlenemedi; yeni kurulum bloke tutuldu.")
            return False

        report["previous_status"] = "failed"
        report["status"] = "closed"
        report["closed_at"] = self.now_stamp()
        report["closed_reason"] = "Yeni kurulum denemesinden once eski failed akis guvenle kapatildi."
        atomic_write_json(report_path, report)
        log(f"Eski failed kurulum kapatildi ve yeni deneme icin temizlendi: {run_id}")
        return True

    def active_workflow_summary(self) -> dict[str, str] | None:
        """Return display-safe details for the recovery card in the main UI."""
        if not self.post_login_state_path().exists():
            return None
        try:
            state = self.load_workflow_state()
        except PermissionError:
            # The workflow file is deliberately protected against a standard
            # target account. An elevated operator is different: the file ACL
            # explicitly grants Administrators, so repair an interrupted ACL
            # inheritance/replace operation and retry once instead of showing
            # an endless "start as administrator" prompt.
            if self.is_admin_session():
                try:
                    self.repair_workflow_state_acl_for_administrator()
                    state = self.load_workflow_state()
                except Exception as exc:  # noqa: BLE001 - retain a safe recovery card
                    return {
                        "run_id": "",
                        "status": "invalid",
                        "target_username": "",
                        "target_user_type": "",
                        "created_at": "",
                        "report_status": "",
                        "detail": (
                            "Yonetici oturumu korumali is akisi dosyasini acamadi: "
                            + self._redact_diagnostic_text(exc, limit=260)
                        ),
                    }
            else:
                return {
                    "run_id": "",
                    "status": "automatic",
                    "target_username": "",
                    "target_user_type": "",
                    "created_at": "",
                    "report_status": "",
                    "detail": (
                        "Korumali post-login plani normal kullaniciya acilmaz. "
                        "Ikinci faz bu oturumda otomatik calisir; ek bir islem gerekmiyor."
                    ),
                }
        except Exception as exc:  # noqa: BLE001 - the UI must offer cleanup
            return {
                "run_id": "",
                "status": "invalid",
                "target_username": "",
                "created_at": "",
                "report_status": "",
                "detail": self._redact_diagnostic_text(exc, limit=320),
            }

        run_id = str(state.get("run_id", "")).strip()
        report_status = ""
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            report_path = self.report_output_dir() / f"{run_id}.json"
            try:
                report_status = str(read_json(report_path).get("status", "")).strip()
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return {
            "run_id": run_id,
            "status": workflow_status(state),
            "target_username": str(state.get("target_username", "")).strip(),
            "target_user_type": str(state.get("target_user_type", "")).strip(),
            "created_at": str(state.get("created_at", "")).strip(),
            "report_status": report_status,
            "detail": "",
        }

    def _workflow_state_is_readable(self, state_path: Path) -> bool:
        """Probe the one protected document without parsing or exposing it."""
        try:
            with state_path.open("rb") as handle:
                handle.read(1)
        except OSError:
            return False
        return True

    def repair_workflow_state_acl_for_administrator(self) -> None:
        """Restore the documented SYSTEM/Administrators ACL for recovery only.

        icacls with recursive continuation can report a nonzero aggregate
        result when an unrelated child is locked, even after it has fixed the
        pending plan. Recovery must judge the actual protected plan's
        readability, not that aggregate return code.
        """
        if sys.platform != "win32" or not self.is_admin_session():
            raise RuntimeError("Korumali is akisi ACL'i yalnizca yukseltilmis yoneticiyle onarilabilir.")
        state_path = self.post_login_state_path()
        if not state_path.is_file():
            raise RuntimeError("Korumali post-login plani bulunamadi.")
        directory = state_path.parent
        directory_acl = [
            "icacls.exe",
            str(directory),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)(F)",
            "*S-1-5-32-544:(OI)(CI)(F)",
            "/t",
            "/c",
        ]
        state_acl = [
            "icacls.exe",
            str(state_path),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
            "/c",
        ]

        first_directory = self._run_quiet(directory_acl)
        first_state = self._run_quiet(state_acl)
        if self._workflow_state_is_readable(state_path):
            return

        # An interrupted ACL replacement can leave the directory owned by an
        # identity that even a member of Administrators cannot modify. This
        # path is elevated recovery only: reclaim ownership, then restore the
        # strict SYSTEM/Administrators ACL without granting target-user access.
        ownership = self._run_quiet(
            ["takeown.exe", "/f", str(directory), "/r", "/d", "y"]
        )
        second_directory = self._run_quiet(directory_acl)
        second_state = self._run_quiet(state_acl)
        if self._workflow_state_is_readable(state_path):
            return
        raise RuntimeError(
            "Korumali is akisi klasorunun SYSTEM/Administrators ACL'i onarilamadi "
            f"(ilk={first_directory.returncode}/{first_state.returncode}, "
            f"sahiplik={ownership.returncode}, "
            f"tekrar={second_directory.returncode}/{second_state.returncode})."
        )

    def restore_local_account_picker(self, log: Logger) -> None:
        """Return Windows to the normal local-account picker after X removal.

        Windows does not enumerate local accounts on a domain-joined device by
        default. Keep the normal account tiles and Switch User visible, then
        enable the documented device policy that enumerates local accounts.
        """
        script = """
$ErrorActionPreference = 'Stop'
$signInPath = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System'
$logonPolicyPath = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\System'
New-Item -Path $signInPath -Force | Out-Null
New-Item -Path $logonPolicyPath -Force | Out-Null
Set-ItemProperty -Path $signInPath -Name 'DontDisplayLastUserName' -Type DWord -Value 0 -Force
Set-ItemProperty -Path $signInPath -Name 'HideFastUserSwitching' -Type DWord -Value 0 -Force
Set-ItemProperty -Path $logonPolicyPath -Name 'EnumerateLocalUsers' -Type DWord -Value 1 -Force
$signIn = Get-ItemProperty -LiteralPath $signInPath -ErrorAction Stop
$logon = Get-ItemProperty -LiteralPath $logonPolicyPath -ErrorAction Stop
if ([int]$signIn.DontDisplayLastUserName -ne 0 -or [int]$signIn.HideFastUserSwitching -ne 0 -or [int]$logon.EnumerateLocalUsers -ne 1) {
    throw 'Yerel kullanici secim ekrani ilkeleri dogrulanamadi.'
}
"""
        self.run_powershell(script, log)
        log("Yerel kullanici secim ekrani ve domain cihazlarda yerel hesap listesi geri yuklendi.")

    def start_scheduled_tasks_now(self, task_names: list[str], log: Logger) -> None:
        """Start only durable ACIK tasks already bound to the target account."""
        safe_names = [
            name
            for name in task_names
            if re.fullmatch(r"AcikOnboarding(?:UserPhase|Finalize)-[A-Za-z0-9_-]{4,48}", name)
        ]
        if len(safe_names) != len(task_names) or not safe_names:
            raise RuntimeError("Bekleyen ikinci faz gorev adi gecersiz.")
        names_literal = ", ".join(f"'{self.ps_escape(name)}'" for name in safe_names)
        script = f"""
$ErrorActionPreference = 'Stop'
$taskNames = @({names_literal})
foreach ($taskName in $taskNames) {{
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {{ throw "Bekleyen gorev bulunamadi: $taskName" }}
    if ($task.State -ne 'Running') {{ Start-ScheduledTask -TaskName $taskName }}
}}
"""
        self.run_powershell(script, log)
        log("Bekleyen ikinci faz gorevleri hedef oturum icin baslatildi: " + ", ".join(safe_names))

    def resume_active_workflow(self, log: Logger) -> list[UiMessage]:
        """Resume the recorded workflow instead of merely opening its report.

        The manual UI always runs elevated, so it must not execute a
        standard-user action in its own identity. It verifies that the
        interactive desktop belongs to the stored target and starts only the
        durable tasks which were already registered for that account.
        """
        try:
            state = self.load_workflow_state()
        except PermissionError as exc:
            if not self.is_admin_session():
                raise RuntimeError(
                    "Korumali post-login plani normal kullaniciya acilmaz. "
                    "Ikinci faz otomatik baslatilir; uygulamayi yonetici olarak acmadan "
                    "manuel devam islemi yapilamaz."
                ) from exc
            # The recovery card already repairs this ACL for an elevated
            # operator. Continue must use the same recovery path; otherwise
            # the card is visible but its action always fails with the normal
            # user warning even though the EXE is running as administrator.
            try:
                self.repair_workflow_state_acl_for_administrator()
                state = self.load_workflow_state()
            except Exception as repair_exc:  # noqa: BLE001 - show a precise recovery failure
                raise RuntimeError(
                    "Yonetici oturumu korumali post-login planinin ACL onarimini tamamlayamadi. "
                    "Ayrintili hata USB tanilama kaydina yazildi."
                ) from repair_exc
        run_id = str(state.get("run_id", "")).strip()
        target_username = str(state.get("target_username", "")).strip()
        target_user_type = str(state.get("target_user_type", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            raise RuntimeError("Bekleyen kurulumun run_id degeri gecersiz.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", target_username):
            raise RuntimeError("Bekleyen kurulumun hedef kullanicisi gecersiz.")

        options = state.get("options", {})
        delete_x_user = isinstance(options, dict) and bool(options.get("delete_x_user"))
        interactive_identity = self.get_interactive_username(log).strip()
        target_principal = str(state.get("target_principal", "")).strip()
        interactive_username = interactive_identity.rsplit("\\", 1)[-1].casefold()
        if target_user_type.casefold() == "domain":
            # Domain recovery starts from a generic logon trigger while the
            # workstation trust is unavailable.  A local account with the
            # same short name must not resume the domain workflow.
            identity_matches = bool(
                interactive_identity
                and target_principal
                and interactive_identity.casefold() == target_principal.casefold()
            )
        else:
            identity_matches = interactive_username == target_username.casefold()
        if not identity_matches:
            if delete_x_user and self.is_admin_session():
                # A previously registered V5.7 SYSTEM cleanup can fail to
                # launch on a damaged scheduler image.  Do not force the
                # operator to close the protected workflow and reinstall:
                # preserve its state, move to the intended account, and let
                # the existing SYSTEM finalizer resume and verify X cleanup.
                handoff_target = self.restart_pending_workflow_for_handoff(log)
                return [
                    (
                        "Bilgi",
                        "Bekleyen X temizleme akisi korunarak hedef kullaniciya gecis icin "
                        f"yeniden baslatma planlandi. Hedef: {handoff_target}.",
                        "info",
                    )
                ]
            login_hint = (
                f"Yerel hesap icin normal Windows kullanici secicisinden {target_username} hesabiyla giris yapin."
                if target_user_type.casefold() == "lokal"
                else "Hedef domain kullanicisi ile Windows oturumu acin."
            )
            raise RuntimeError(
                "Surece devam etmek icin hedef kullanici ile oturum acilmalidir. "
                f"Hedef: {target_username}; mevcut: {interactive_identity or 'bilinmiyor'}. {login_hint}"
            )

        task_names: list[str] = []
        if enabled_phase_tasks(state, "user"):
            user_task_name = str(state.get("user_task_name", "")).strip()
            if user_task_name:
                task_names.append(user_task_name)
            elif target_user_type.casefold() == "domain":
                log(
                    "Domain kullanici fazi zamanlanmis gorev yerine hedef "
                    "oturumdaki Startup yardimcisi ile baslatilacak."
                )
            else:
                raise RuntimeError("Kullanici fazi gorevi durum dosyasinda bulunamadi.")
        if enabled_phase_tasks(state, "system"):
            system_task_name = str(state.get("system_task_name", "")).strip()
            if not system_task_name:
                raise RuntimeError("SYSTEM finalizasyon gorevi durum dosyasinda bulunamadi.")
            task_names.append(system_task_name)
        if not task_names:
            raise RuntimeError("Bekleyen kurulumda baslatilacak ikinci faz gorevi yok.")

        if target_user_type.casefold() == "lokal":
            self.restore_local_account_picker(log)
        if enabled_phase_tasks(state, "user"):
            self.install_post_login_startup_helper(run_id, target_username, log)
        self.start_scheduled_tasks_now(task_names, log)
        return [
            (
                "Bilgi",
                "Bekleyen ikinci faz gorevleri hedef kullanici icin baslatildi. "
                "Rapor kisa sure icinde guncellenecek.",
                "info",
            )
        ]

    def close_active_workflow_for_retry(self, log: Logger) -> str:
        """Explicitly end the displayed old workflow and retain its report."""
        try:
            state = self.load_workflow_state()
        except Exception as exc:  # noqa: BLE001 - cancellation has a safe fallback
            log(f"Onceki durum dosyasi okunamadi; zorunlu temizlik uygulanacak: {exc}")
            self.cancel_active_workflow(log)
            if self.post_login_state_path().exists():
                raise RuntimeError("Bozuk onceki durum dosyasi temizlenemedi.") from exc
            log("Bozuk onceki kurulum kalintilari temizlendi; yeni deneme hazir.")
            return ""
        run_id = str(state.get("run_id", "")).strip()
        report_path: Path | None = None
        report: dict[str, object] | None = None
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            candidate = self.report_output_dir() / f"{run_id}.json"
            if candidate.is_file():
                try:
                    report = read_json(candidate)
                    report_path = candidate
                except Exception as exc:  # noqa: BLE001
                    log(f"Eski rapor okunamadi; durum dosyasi yine de temizlenecek: {exc}")

        self.cancel_active_workflow(log)
        if self.post_login_state_path().exists():
            raise RuntimeError("Onceki kurulumun durum dosyasi temizlenemedi.")

        if report is not None and report_path is not None:
            previous_status = str(report.get("status", "")).strip()
            report["previous_status"] = previous_status
            report["status"] = "closed"
            report["closed_at"] = self.now_stamp()
            report["closed_reason"] = "Operator yeni kurulum denemesi icin eski sureci sonlandirdi."
            atomic_write_json(report_path, report)
        log(f"Onceki kurulum sonlandirildi; yeni deneme hazir: {run_id or 'bilinmeyen run'}")
        return run_id

    def assert_no_active_workflow(self, log: Logger | None = None) -> None:
        path = self.post_login_state_path()
        if not path.exists():
            return
        try:
            existing = self.load_workflow_state()
            run_id = str(existing.get("run_id", ""))
            status = workflow_status(existing)
        except Exception as exc:
            raise RuntimeError(
                "Onceki is akisinin durum dosyasi bozuk. Yeni kurulumla uzerine "
                f"yazilmadi; yonetici incelemesi gerekiyor: {exc}"
            ) from exc
        raise RuntimeError(
            f"Bekleyen bir cihaz kurulumu var (Run ID: {run_id}, durum: {status}). "
            "Yeni kurulum baslatilmadi."
        )

    def cancel_active_workflow(self, log: Logger) -> None:
        """Stop every artifact of a pending run without leaving it blocked."""
        state: dict[str, object] = {}
        try:
            state = self.load_workflow_state()
        except Exception as exc:
            log(f"Bekleyen durum dosyasi okunamadi; zorunlu temizlik uygulanacak: {exc}")

        run_id = str(state.get("run_id", ""))
        user_task_name = str(state.get("user_task_name", ""))
        system_task_name = str(state.get("system_task_name", ""))
        initial_task_name = str(state.get("initial_restart_task_name", ""))
        cleanup_steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("kullanici fazi gorevi", lambda: self.remove_user_phase_task(user_task_name, log)),
            ("SYSTEM finalizasyon gorevi", lambda: self.remove_system_finalize_task(system_task_name, log)),
            ("ilk yeniden baslatma gorevi", lambda: self.remove_initial_restart_task(initial_task_name, log)),
            ("eski baslangic yardimcisi", self.clear_post_login_helper),
            ("kullanici fazi dosyalari", lambda: self.clear_user_phase_artifacts(run_id) if run_id else None),
            ("bekleyen durum dosyasi", self.clear_post_login_state),
        )
        failures: list[str] = []
        for label, action in cleanup_steps:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - cancellation is best effort
                failures.append(f"{label}: {exc}")
        try:
            self.remove_orphaned_onboarding_tasks(log, run_id)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"yetim gorevler: {exc}")
        if failures:
            log("Kurulum iptal edildi; temizlenemeyen kalintilar: " + " | ".join(failures))
        else:
            log("Bekleyen kurulum iptal edildi ve kalintilar temizlendi.")
        return

        try:
            state = self.load_workflow_state()
            run_id = str(state.get("run_id", ""))
            user_task_name = str(state.get("user_task_name", ""))
            task_name = str(state.get("system_task_name", ""))
            
            self.clear_post_login_helper()
            if run_id:
                self.clear_user_phase_artifacts(run_id)
            self.clear_post_login_state()
            if task_name:
                self.remove_system_finalize_task(task_name, log)
            if user_task_name:
                self.remove_user_phase_task(user_task_name, log)
            log("Bekleyen kurulum iptal edildi ve kalintilar temizlendi.")
        except Exception as exc:
            # Durum dosyası okunamıyorsa bile kalıntıları temizle
            self.clear_post_login_state()
            self.clear_post_login_helper()
            log(f"Zorunlu iptal sirasinda bazi hatalar yoksayildi: {exc}")


    def write_workflow_state(self, state: dict[str, object], log: Logger | None = None) -> None:
        validate_state(state)
        state["updated_at"] = utc_now()
        path = self.post_login_state_path()
        atomic_write_json(path, state)
        if sys.platform != "win32" or not self.is_admin_session():
            return

        # The privileged plan is never writable by the target user. User-phase
        # progress is stored in a separate allowlisted document.
        result = self._run_quiet(
            [
                "icacls.exe",
                str(path.parent),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)(F)",
                "*S-1-5-32-544:(OI)(CI)(F)",
                "/t",
                "/c",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("SYSTEM is akisi klasoru ACL ile korunamadi.")

    def load_workflow_state(self) -> dict[str, object]:
        state = read_json(self.post_login_state_path())
        validate_state(state)
        return state

    def runtime_dir(self) -> Path:
        program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
        path = program_data / "AcikOnboarding" / "runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def system_runtime_dir(self) -> Path:
        path = self.runtime_dir() / "system"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def post_login_state_path(self) -> Path:
        return self.system_runtime_dir() / "pending_post_login.json"

    def post_login_result_path(self) -> Path:
        return self.system_runtime_dir() / "post_login_result.json"

    def user_phase_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            raise RuntimeError("Kullanici fazi run_id degeri gecersiz.")
        return self.runtime_dir() / "user" / run_id

    def user_phase_plan_path(self, run_id: str) -> Path:
        return self.user_phase_dir(run_id) / "plan.json"

    def user_phase_progress_path(self, run_id: str) -> Path:
        return self.user_phase_dir(run_id) / "progress.json"

    def user_phase_log_path(self, run_id: str) -> Path:
        return self.user_phase_dir(run_id) / "post_login_helper.log"

    def _protect_user_phase_dir(
        self,
        state: dict[str, object],
        log: Logger,
        grant_target: bool,
    ) -> bool:
        directory = self.user_phase_dir(str(state.get("run_id", "")))
        directory.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            return True
        if not self.is_admin_session():
            raise RuntimeError("Kullanici fazi ACL'i yalnizca SYSTEM veya yonetici ayarlayabilir.")

        base_acl = self._run_quiet(
            [
                "icacls.exe",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)(F)",
                "*S-1-5-32-544:(OI)(CI)(F)",
                "/t",
                "/c",
            ]
        )
        if base_acl.returncode != 0:
            raise RuntimeError("Kullanici fazi klasoru SYSTEM ACL'i ile korunamadi.")
        if not grant_target:
            return True

        target_sid = str(state.get("target_sid", "")).strip()
        principal = (
            f"*{target_sid}"
            if re.fullmatch(r"S-\d(?:-\d+)+", target_sid)
            else str(state.get("target_principal", "")).strip()
        )
        if not principal:
            raise RuntimeError("Kullanici fazi icin hedef principal eksik.")
        target_acl = self._run_quiet(
            [
                "icacls.exe",
                str(directory),
                "/grant:r",
                f"{principal}:(OI)(CI)(M)",
                "/t",
                "/c",
            ]
        )
        if target_acl.returncode != 0:
            log(
                "Kullanici fazi ACL'i henuz hedef hesaba verilemedi. "
                "Domain yeniden baslatmasindan sonra SYSTEM tekrar deneyecek."
            )
            return False
        log(f"Kullanici fazi ACL'i hedef hesaba verildi: {principal}")
        return True

    def write_user_phase_plan(self, state: dict[str, object], log: Logger) -> None:
        """Write an allowlisted user plan without exposing privileged fields."""
        run_id = str(state.get("run_id", ""))
        tasks = state.get("tasks", {})
        if not isinstance(tasks, dict):
            raise RuntimeError("Kullanici fazi gorevleri gecersiz.")
        user_tasks = {
            name: json.loads(json.dumps(tasks.get(name, {})))
            for name in USER_PHASE_TASKS
        }
        plan: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "target_username": state.get("target_username", ""),
            "target_user_type": state.get("target_user_type", ""),
            "target_principal": state.get("target_principal", ""),
            "target_sid": state.get("target_sid", ""),
            "credential_username": state.get("credential_username", ""),
            "credential_password_protected": state.get("credential_password_protected", ""),
            "required_wifi_ssid": state.get("required_wifi_ssid", ""),
            "file_server": json.loads(json.dumps(state.get("file_server", {}))),
            "printer": json.loads(json.dumps(state.get("printer", {}))),
            "desktop_automation": json.loads(
                json.dumps(state.get("desktop_automation", {}))
            ),
            "tasks": user_tasks,
            "phases": {"user": {"status": phase_status(state, "user")}},
            "has_system_tasks": bool(enabled_phase_tasks(state, "system")),
            "created_at": state.get("created_at", utc_now()),
            "updated_at": utc_now(),
        }
        validate_state(plan)
        self._protect_user_phase_dir(state, log, grant_target=False)
        atomic_write_json(self.user_phase_plan_path(run_id), plan)
        self._protect_user_phase_dir(state, log, grant_target=True)

    def write_user_phase_progress(
        self,
        state: dict[str, object],
        _log: Logger | None = None,
    ) -> None:
        """Persist only user-task statuses; privileged plan data is never copied."""
        run_id = str(state.get("run_id", ""))
        tasks = state.get("tasks", {})
        if not isinstance(tasks, dict):
            raise RuntimeError("Kullanici ilerleme gorevleri gecersiz.")
        payload: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "target_username": state.get("target_username", ""),
            "tasks": {
                name: {
                    "enabled": bool(tasks.get(name, {}).get("enabled")),
                    "status": str(tasks.get(name, {}).get("status", TASK_PENDING)),
                    "attempts": int(tasks.get(name, {}).get("attempts", 0)),
                    "error": str(tasks.get(name, {}).get("error", ""))[:2000],
                    "updated_at": str(tasks.get(name, {}).get("updated_at", utc_now())),
                }
                for name in USER_PHASE_TASKS
                if isinstance(tasks.get(name), dict)
            },
            "phase_status": phase_status(state, "user"),
            "messages": [
                list(message)
                for message in state.get("user_messages", [])
                if isinstance(message, (list, tuple)) and len(message) == 3
            ],
            "updated_at": utc_now(),
        }
        atomic_write_json(self.user_phase_progress_path(run_id), payload)

    def merge_user_phase_progress(
        self,
        state: dict[str, object],
        progress: dict[str, object],
    ) -> None:
        """Merge only allowlisted user status fields into the SYSTEM-owned plan."""
        if str(progress.get("run_id", "")) != str(state.get("run_id", "")):
            raise RuntimeError("Kullanici ilerleme run_id degeri uyusmuyor.")
        if str(progress.get("target_username", "")).casefold() != str(
            state.get("target_username", "")
        ).casefold():
            raise RuntimeError("Kullanici ilerleme hedefi uyusmuyor.")
        progress_tasks = progress.get("tasks", {})
        state_tasks = state.get("tasks", {})
        if not isinstance(progress_tasks, dict) or not isinstance(state_tasks, dict):
            raise RuntimeError("Kullanici ilerleme gorevleri gecersiz.")
        allowed_statuses = {
            TASK_PENDING,
            TASK_RUNNING,
            TASK_SUCCEEDED,
            TASK_RETRYABLE_FAILED,
            TASK_PERMANENT_FAILED,
            TASK_SKIPPED,
        }
        for name in USER_PHASE_TASKS:
            source = progress_tasks.get(name)
            target = state_tasks.get(name)
            if not isinstance(source, dict) or not isinstance(target, dict):
                continue
            status = str(source.get("status", ""))
            if status not in allowed_statuses:
                raise RuntimeError(f"Kullanici ilerleme durumu gecersiz: {name}")
            # The immutable plan decides whether a task exists. The user result
            # may only report status, bounded attempts and an error message.
            if not bool(target.get("enabled")):
                continue
            target["status"] = status
            target["attempts"] = min(3, max(0, int(source.get("attempts", 0))))
            target["error"] = str(source.get("error", ""))[:2000]
            target["updated_at"] = str(source.get("updated_at", utc_now()))
        state["user_messages"] = [
            list(message)
            for message in progress.get("messages", [])
            if isinstance(message, list) and len(message) == 3
        ][:50]
        self._set_phase_state(state, "user")

    def finalize_retryable_phase_tasks(
        self,
        state: dict[str, object],
        phase: str,
        log: Logger,
        reason: str,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        """Make non-critical one-shot failures terminal for this onboarding run.

        User-facing extras must never keep mandatory X cleanup in an endless
        ``pending`` state.  The X task is excluded because its verified account
        and profile deletion is retryable by design.
        """
        excluded = exclude or set()
        tasks = state.get("tasks", {})
        if not isinstance(tasks, dict):
            return
        for name in enabled_phase_tasks(state, phase):
            if name in excluded:
                continue
            task = tasks.get(name)
            if not isinstance(task, dict):
                continue
            if str(task.get("status", "")) in {TASK_PENDING, TASK_RUNNING, TASK_RETRYABLE_FAILED}:
                mark_task(state, name, TASK_PERMANENT_FAILED, reason)
                log(f"Tek seferlik {phase} gorevi sonlandirildi: {name} - {reason}")
        self._set_phase_state(state, phase)

    def load_user_phase_state(self, run_id: str, log: Logger) -> dict[str, object]:
        plan_path = self.user_phase_plan_path(run_id)
        last_error: Exception | None = None
        # On a domain join the logon helper and SYSTEM ACL repair start together.
        # Wait for the SYSTEM reconciliation task instead of giving up while
        # the target account's SID/ACL is still being materialised.
        for attempt in range(1, 31):
            try:
                state = read_json(plan_path)
                validate_state(state)
                if str(state.get("run_id", "")) != run_id:
                    raise RuntimeError("Kullanici fazi run_id degeri uyusmuyor.")
                progress_path = self.user_phase_progress_path(run_id)
                if progress_path.exists():
                    self.merge_user_phase_progress(state, read_json(progress_path))
                return state
            except (FileNotFoundError, PermissionError, OSError) as exc:
                last_error = exc
                if attempt == 1 or attempt % 10 == 0:
                    log(
                        "Kullanici fazi plani SYSTEM ACL hazirligini bekliyor "
                        f"({attempt}/30): {exc}"
                    )
                time.sleep(2)
        raise RuntimeError(f"Kullanici fazi plani okunamadi: {last_error}")

    def startup_helper_path(self) -> Path:
        program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
        return program_data / "Microsoft/Windows/Start Menu/Programs/Startup/AcikPostLogin.cmd"

    def post_login_python_path(self) -> Path:
        current_exe = Path(sys.executable)
        pythonw = current_exe.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
        return current_exe

    def build_network_username(self, username: str) -> str:
        domain_prefix = self.config.network_resources.credential_domain.strip() or self.config.domain.name.strip()
        if not domain_prefix:
            return username
        return f"{domain_prefix}\\{username}"

    def build_unc_path(self, host: str, share: str) -> str:
        return f"\\\\{host}\\{share}"

    def get_connected_wifi_ssid(self) -> str:
        completed = self._run_quiet(["netsh.exe", "wlan", "show", "interfaces"])
        if completed.returncode != 0:
            return ""
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if not line or "BSSID" in line or not line.startswith("SSID"):
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
        return ""

    def ensure_required_wifi_connection(self, log: Logger) -> None:
        required_ssid = (
            self.config.network_resources.required_wifi_ssid.strip()
            or self.config.wifi_profiles.get("domain_join", WifiProfile("", "")).ssid.strip()
            or self.config.wifi_profiles.get("general", WifiProfile("", "")).ssid.strip()
        )
        if not required_ssid:
            log("Zorunlu Wi-Fi adi tanimli degil. Mevcut baglanti ile devam ediliyor.")
            return

        for attempt in range(1, 7):
            current_ssid = self.get_connected_wifi_ssid()
            if current_ssid.lower() == required_ssid.lower():
                log(f"Wi-Fi dogrulandi (Deneme {attempt}): {required_ssid}")
                return

            log(f"Wi-Fi baglantisi '{required_ssid}' agina alinmaya calisiliyor (Deneme {attempt}/6)...")
            self._run(["netsh.exe", "wlan", "connect", f"name={required_ssid}", f"ssid={required_ssid}"], log, check=False)
            time.sleep(5)

        current_ssid = self.get_connected_wifi_ssid()
        if current_ssid.lower() != required_ssid.lower():
            raise RuntimeError(f"Zorunlu Wi-Fi baglantisi saglanamadi: {required_ssid}")
        log(f"Wi-Fi baglantisi hazir: {required_ssid}")

    def connect_network_resource(
        self,
        unc_path: str,
        username: str,
        password: str,
        log: Logger,
        resource_type: int = 1,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Ag kaynagi baglantisi yalnizca Windows'ta destekleniyor.")
        if not unc_path.startswith("\\\\") or not username or not password:
            raise RuntimeError("Ag kaynagi veya kimlik bilgileri eksik.")

        class NetResource(ctypes.Structure):
            _fields_ = [
                ("dwScope", ctypes.c_ulong),
                ("dwType", ctypes.c_ulong),
                ("dwDisplayType", ctypes.c_ulong),
                ("dwUsage", ctypes.c_ulong),
                ("lpLocalName", ctypes.c_wchar_p),
                ("lpRemoteName", ctypes.c_wchar_p),
                ("lpComment", ctypes.c_wchar_p),
                ("lpProvider", ctypes.c_wchar_p),
            ]

        resource = NetResource(
            0,
            resource_type,
            0,
            0,
            None,
            unc_path,
            None,
            None,
        )
        result = ctypes.windll.mpr.WNetAddConnection2W(
            ctypes.byref(resource),
            password,
            username,
            0x1,  # CONNECT_UPDATE_PROFILE
        )
        if result in {0, 85}:
            log(f"Windows ag baglantisi dogrulandi: {unc_path}")
            return
        if result == 1219:
            raise RuntimeError(
                "Ayni sunucuya farkli kimlik bilgileriyle acik bir baglanti var. "
                "Mevcut baglantiyi kapatip tekrar deneyin."
            )
        raise RuntimeError(f"Windows ag baglantisi kurulamadi (kod {result}): {unc_path}")

    def create_desktop_shortcut(self, target_path: str, shortcut_name: str, log: Logger) -> None:
        script = f"""
$desktop = [System.Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop '{self.ps_escape(shortcut_name)}.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = '{self.ps_escape(target_path)}'
$shortcut.IconLocation = "$env:SystemRoot\\System32\\imageres.dll,3"
$shortcut.Save()
"""
        self.run_powershell(script, log)
        log(f"Masaustu kisayolu olusturuldu: {shortcut_name}")

    def desktop_path_for_current_user(self) -> Path:
        user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        return user_profile / "Desktop"

    def resolve_signature_source(self, company_name: str) -> Path:
        base = self.config.desktop_automation.signature_source_dir
        if not base or str(base) == "." or not base.exists():
            raise RuntimeError("Imza kaynagi tanimli degil.")
        company_dir = base / company_name
        if company_name and company_dir.exists():
            return company_dir
        return base

    def resolve_wallpaper_source(self) -> Path:
        packaged_asset = self.config.base_dir / "assets" / "wallpaper.jpg"
        if packaged_asset.is_file():
            return packaged_asset
        source = self.config.desktop_automation.wallpaper_source_path
        if source and str(source) != "." and source.exists():
            return source
        if not source or str(source) == ".":
            raise RuntimeError("Duvar kagidi kaynagi tanimli degil.")
        raise RuntimeError(f"Duvar kagidi kaynagi bulunamadi: {source}")

    def resolve_wallpaper_target(self) -> Path:
        """Use the active application package asset directly.

        The installer is portable; when it runs from a USB drive this resolves
        to that drive.  Legacy C: targets are deliberately ignored so a stale
        configuration can never redirect the wallpaper back to an old setup.
        """
        return self.resolve_wallpaper_source()

    def prepare_wallpaper_asset(self, log: Logger) -> Path:
        source = self.resolve_wallpaper_source()
        try:
            with source.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise RuntimeError(
                f"Duvar kagidi USB kaynagi okunamiyor: {source}"
            ) from exc
        log(f"Duvar kagidi aktif uygulama/USB kaynagindan kullanilacak: {source}")
        return source

    def resolve_lock_screen_source(self) -> Path:
        packaged_asset = self.config.base_dir / "assets" / "uyku modu.jpg"
        if packaged_asset.is_file():
            return packaged_asset
        source = self.config.desktop_automation.lock_screen_source_path
        if source and str(source) != "." and source.exists():
            return source
        if not source or str(source) == ".":
            raise RuntimeError("Kilit ekrani gorsel kaynagi tanimli degil.")
        raise RuntimeError(f"Kilit ekrani gorsel kaynagi bulunamadi: {source}")

    def resolve_lock_screen_target(self) -> Path:
        return self.resolve_lock_screen_source()

    def prepare_lock_screen_asset(self, log: Logger) -> Path:
        """Validate the package lock-screen image before the next logon."""
        source = self.resolve_lock_screen_source()
        try:
            with source.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise RuntimeError(
                f"Kilit ekrani USB kaynagi okunamiyor: {source}"
            ) from exc
        log(f"Kilit ekrani gorseli aktif uygulama/USB kaynagindan kullanilacak: {source}")
        return source

    def lock_screen_runtime_dir(self) -> Path:
        """Return the local, pre-logon-readable home for the lock-screen image."""
        program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
        return program_data / "AcikOnboarding" / "lock-screen"

    def stage_lock_screen_asset(self, source: Path, log: Logger) -> Path:
        """Copy the selected image to a local path readable before user sign-in.

        LockApp runs before a normal user session and cannot depend on the USB
        drive or on the protected post-login application directory.  Keep this
        one image in a dedicated ProgramData folder with explicit read access
        for the Windows service and app-container identities that render the
        lock screen.
        """
        if not source.is_file():
            raise RuntimeError(f"Kilit ekrani kaynak dosyasi bulunamadi: {source}")
        if not self.is_admin_session():
            raise RuntimeError("Kilit ekrani gorselini yerlestirmek icin SYSTEM yetkisi gerekli.")

        target_dir = self.lock_screen_runtime_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "uyku modu.jpg"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(temporary.read_bytes()).digest():
                raise RuntimeError("Kilit ekrani gorseli yerel kopyada dogrulanamadi.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        if sys.platform == "win32":
            result = self._run_quiet(
                [
                    "icacls.exe",
                    str(target_dir),
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-18:(OI)(CI)(F)",
                    "*S-1-5-32-544:(OI)(CI)(F)",
                    "*S-1-5-32-545:(OI)(CI)(RX)",
                    "*S-1-5-19:(OI)(CI)(RX)",
                    "*S-1-5-20:(OI)(CI)(RX)",
                    "*S-1-15-2-1:(OI)(CI)(RX)",
                    "/t",
                    "/c",
                ]
            )
            if result.returncode != 0:
                raise RuntimeError("Kilit ekrani gorseli pre-logon ACL ile korunamadi.")

        if not target.is_file():
            raise RuntimeError("Kilit ekrani gorseli yerel hedefte bulunamadi.")
        log(f"Kilit ekrani gorseli pre-logon yerel konuma hazirlandi: {target}")
        return target

    def apply_wallpaper_for_current_user(self, wallpaper_path: Path, lock_change: bool, log: Logger) -> None:
        if not wallpaper_path.exists():
            raise RuntimeError(f"Duvar kagidi hedef dosyasi bulunamadi: {wallpaper_path}")
        
        wallpaper_str = str(wallpaper_path.resolve())
        log(f"Duvar kagidi ayarlaniyor (Saf Python): {wallpaper_str}")

        import winreg

        # HKCU\Control Panel\Desktop - Normal Ayarlar.  CreateKeyEx is used
        # instead of OpenKey because a new or restricted profile may not have
        # the key materialised yet.
        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Control Panel\Desktop",
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            ) as key:
                winreg.SetValueEx(key, "Wallpaper", 0, winreg.REG_SZ, wallpaper_str)
                winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, "10") # Fill (Sığdır/Doldur)
                winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
            log("HKCU Control Panel Desktop kayitlari guncellendi.")
        except Exception as exc:
            raise RuntimeError(
                f"HKCU Control Panel Desktop kayitlari guncellenemedi: {exc}"
            ) from exc

        # Politikalar (Gecici olarak eski engelleri temizle ki degisim uygulanabilsin)
        policy_sys_path = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"
        policy_ad_path = r"Software\Microsoft\Windows\CurrentVersion\Policies\ActiveDesktop"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, policy_sys_path, 0, winreg.KEY_SET_VALUE) as key_sys:
                try: winreg.DeleteValue(key_sys, "Wallpaper")
                except OSError: pass
                try: winreg.DeleteValue(key_sys, "WallpaperStyle")
                except OSError: pass
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, policy_ad_path, 0, winreg.KEY_SET_VALUE) as key_ad:
                try: winreg.DeleteValue(key_ad, "NoChangingWallPaper")
                except OSError: pass
        except OSError:
            pass

        # Windows API ile arka planı anında yenile.  The registry postcondition
        # below is authoritative; a transient Explorer refresh failure must not
        # turn a successfully applied wallpaper into a failed onboarding step.
        try:
            # SPI_SETDESKWALLPAPER (20), SPIF_UPDATEINIFILE (1) | SPIF_SENDCHANGE (2) -> 3
            result = ctypes.windll.user32.SystemParametersInfoW(20, 0, wallpaper_str, 3)
            if result:
                log("SystemParametersInfoW ile arka plan yenilendi.")
            else:
                log("SystemParametersInfoW anlik yenileme yapamadi; sonraki Explorer yenilemesinde uygulanacak.")
        except Exception as exc:
            log(f"SystemParametersInfoW anlik yenilemesi atlandi: {exc}")

        # UpdatePerUserSystemParameters cagrisi ile arayuzu zorla yenile
        try:
            creationflags = 0x08000000 if sys.platform == "win32" else 0
            subprocess.run(["rundll32.exe", "user32.dll", "UpdatePerUserSystemParameters"], check=False, creationflags=creationflags)
        except Exception:
            pass

        # Kilitleme politikalarini simdi uygula (duvar kagidi degistikten sonra kilitlemek icin)
        if lock_change:
            try:
                # System policy (Zorla degistirilememe kilidi)
                key_sys, _ = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, policy_sys_path, 0, winreg.KEY_SET_VALUE)
                with key_sys:
                    winreg.SetValueEx(key_sys, "Wallpaper", 0, winreg.REG_SZ, wallpaper_str)
                    winreg.SetValueEx(key_sys, "WallpaperStyle", 0, winreg.REG_SZ, "10")
                    # Standart NoChangingWallPaper kilidini HKCU\..\Policies\System altına da ekliyoruz
                    winreg.SetValueEx(key_sys, "NoChangingWallPaper", 0, winreg.REG_DWORD, 1)
                
                # ActiveDesktop policy
                key_ad, _ = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, policy_ad_path, 0, winreg.KEY_SET_VALUE)
                with key_ad:
                    winreg.SetValueEx(key_ad, "NoChangingWallPaper", 0, winreg.REG_DWORD, 1)
                log("Duvar kagidi kilitleme politikalari uygulandi.")
            except Exception as exc:
                # A domain GPO can make these keys read-only for a standard
                # user.  Wallpaper application is still valid in that case;
                # only the optional end-user lock is skipped and recorded.
                log(f"Duvar kagidi kilitleme politikasi uygulanamadi, kilit atlandi: {exc}")

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Control Panel\Desktop",
            0,
            winreg.KEY_READ,
        ) as key:
            actual_wallpaper, _ = winreg.QueryValueEx(key, "Wallpaper")
        if Path(str(actual_wallpaper)).resolve() != Path(wallpaper_str).resolve():
            raise RuntimeError("Duvar kagidi registry son kosulu dogrulanamadi.")
        
        if lock_change:
            log(f"Kurumsal arka plan uygulandi ve degistirme kilidi aktif edildi: {wallpaper_path}")
        else:
            # Local-standard-user workflows deliberately defer the lock to the
            # SYSTEM finalizer. The image must first be set inside the real
            # interactive session, otherwise Windows can postpone the visual
            # change until the following Explorer refresh.
            log(
                "Kurumsal arka plan hedef kullanici oturumunda uygulandi; "
                "SYSTEM kalici degistirme ilkesini sonraki adimda dogrulayacak: "
                f"{wallpaper_path}"
            )

    def deploy_signature_assets(self, company_name: str, log: Logger) -> None:
        source_dir = self.resolve_signature_source(company_name)
        target_dir = self.desktop_path_for_current_user() / self.config.desktop_automation.signature_folder_name.strip()
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in source_dir.iterdir():
            destination = target_dir / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(item, destination)
        log(f"Imza dosyalari kopyalandi: {target_dir}")

    def launch_classic_outlook(self, log: Logger) -> None:
        desktop = self.config.desktop_automation
        outlook_path = desktop.outlook_classic_path.strip()
        if not outlook_path:
            raise RuntimeError("Outlook Classic yolu tanimli degil.")
        executable = self._resolve_tool_path(outlook_path)
        if not executable.exists():
            raise RuntimeError(f"Outlook Classic bulunamadi: {executable}")
        subprocess.Popen([str(executable)], cwd=str(executable.parent))
        log(f"Outlook Classic baslatildi: {executable}")
        log("Outlook hesap otomasyonu icin temel hazir. Gerekirse sonraki turda UI otomasyonu eklenebilir.")

    def run_powershell(
        self,
        script: str,
        log: Logger,
        check: bool = True,
        timeout_seconds: int = 300,
        extra_secrets: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        temp_script_path: Path | None = None
        try:
            # CREATE_NO_WINDOW combined with `-Command -` can silently discard
            # multi-line stdin on hardened Windows images.  A short-lived UTF-8
            # script file keeps the console hidden and preserves stdout/stderr.
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".ps1",
                encoding="utf-8-sig",
                delete=False,
            ) as handle:
                handle.write(script)
                temp_script_path = Path(handle.name)
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(temp_script_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=creationflags,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"PowerShell {timeout_seconds} saniyede tamamlanmadi."
            ) from exc
        finally:
            if temp_script_path is not None:
                temp_script_path.unlink(missing_ok=True)
        if completed.stdout.strip():
            log(self._redact_diagnostic_text(completed.stdout.strip(), limit=1800, extra_secrets=extra_secrets))
        if completed.stderr.strip():
            log(self._redact_diagnostic_text(completed.stderr.strip(), limit=1800, extra_secrets=extra_secrets))
        if check and completed.returncode != 0:
            raw_detail = completed.stderr.strip() or completed.stdout.strip()
            detail = self._redact_diagnostic_text(raw_detail, limit=700, extra_secrets=extra_secrets)
            suffix = f" Ayrinti: {detail}" if detail else ""
            raise RuntimeError(
                f"PowerShell komutu basarisiz oldu (kod {completed.returncode}).{suffix}"
            )
        return completed

    def create_or_update_local_user(self, full_name: str, username: str, password: str, log: Logger) -> None:
        if not full_name or not username or not password:
            raise RuntimeError("Ad soyad, kullanici adi ve sifre bos olamaz.")

        script = f"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop
$userName = '{self.ps_escape(username)}'
$fullName = '{self.ps_escape(full_name)}'
$managedDescription = 'ACIK-Onboarding managed account'
$securePassword = ConvertTo-SecureString '{self.ps_escape(password)}' -AsPlainText -Force
$existing = Get-LocalUser -Name $userName -ErrorAction SilentlyContinue
if ($null -eq $existing) {{
    New-LocalUser -Name $userName -Password $securePassword -FullName $fullName -Description $managedDescription -PasswordNeverExpires -AccountNeverExpires
}} else {{
    if ($existing.Description -ne $managedDescription -or $existing.FullName -ne $fullName) {{
        throw "Ayni kullanici adi uygulama disinda olusturulmus: $userName"
    }}
    # Set-LocalUser differs from New-LocalUser: on current Windows it needs
    # an explicit Boolean argument for PasswordNeverExpires.
    Set-LocalUser -Name $userName -Password $securePassword -FullName $fullName -PasswordNeverExpires $true -AccountNeverExpires
}}

# Kullanicinin giris ekraninda gorunebilmesi icin yerel "Users" (Kullanicilar) grubuna eklenmesi gerekir.
# Dil bagimsiz olmasi icin grubun adi SID (S-1-5-32-545) uzerinden cozumlenir.
$usersSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-545")
$usersGroupName = $usersSid.Translate([System.Security.Principal.NTAccount]).Value.Split('\\')[-1]
try {{
    Add-LocalGroupMember -Group $usersGroupName -Member $userName -ErrorAction Stop
}} catch {{
    if ($_.Exception.Message -notmatch 'already a member') {{
        throw
    }}
}}
$verified = Get-LocalUser -Name $userName -ErrorAction Stop
if (-not $verified.Enabled -or $verified.Description -ne $managedDescription) {{
    throw 'Yerel kullanici son kosulu dogrulanamadi.'
}}
"""
        self.run_powershell(script, log, extra_secrets=(password,))
        log(f"Yerel kullanici hazir: {username}")

    def prepare_local_admin(self, log: Logger) -> None:
        """Prepare the SID-500 account without disabling the last administrator."""
        username = self.config.tools.local_admin_username.strip()
        password = self.config.tools.local_admin_password
        if not username or not password:
            raise RuntimeError("Lokaladm ayarlari eksik.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", username):
            raise RuntimeError("Lokaladm kullanici adi Windows kurallarina uygun degil.")

        script = f"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop
$targetName = '{self.ps_escape(username)}'
$securePassword = ConvertTo-SecureString '{self.ps_escape(password)}' -AsPlainText -Force
$builtIn = Get-LocalUser | Where-Object {{ $_.SID.Value -match '-500$' }} | Select-Object -First 1
if ($null -eq $builtIn) {{
    throw 'SID-500 yerlesik Administrator hesabi bulunamadi.'
}}
$conflict = Get-LocalUser -Name $targetName -ErrorAction SilentlyContinue
if ($conflict -and $conflict.SID.Value -ne $builtIn.SID.Value) {{
    throw "Lokaladm adi baska bir yerel hesap tarafindan kullaniliyor: $targetName"
}}
if ($builtIn.Name -ne $targetName) {{
    Rename-LocalUser -Name $builtIn.Name -NewName $targetName -ErrorAction Stop
}}
Set-LocalUser -Name $targetName -Password $securePassword -PasswordNeverExpires $true -ErrorAction Stop
Enable-LocalUser -Name $targetName -ErrorAction Stop
$adminSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$adminGroup = $adminSid.Translate([System.Security.Principal.NTAccount]).Value.Split('\\')[-1]
try {{
    Add-LocalGroupMember -Group $adminGroup -Member $targetName -ErrorAction Stop
}} catch {{
    # Get-LocalGroupMember can fail with 1789 when a stale domain SID is
    # present in the local Administrators group.  The mutation above is the
    # authoritative membership operation: success means this SID-500 account
    # was added, while the documented "already a member" response means it
    # was already present.  Do not enumerate every member of this group here.
    if ($_.Exception.Message -notmatch 'already a member|zaten.*uye|1378') {{ throw }}
}}
$verified = Get-LocalUser -Name $targetName -ErrorAction Stop
if (-not $verified.Enabled -or $verified.SID.Value -notmatch '-500$') {{
    throw 'Lokaladm dogrulamasi basarisiz.'
}}
Write-Output "Lokaladm verified: $($verified.SID.Value)"
"""
        self.run_powershell(script, log, extra_secrets=(password,))
        log(f"Lokaladm dogrulandi: {username}")

    def _group_sid(self, group_name: str) -> str:
        sids = {
            "Administrators": "S-1-5-32-544",
            "Network Configuration Operators": "S-1-5-32-556",
        }
        if group_name not in sids:
            raise ValueError(f"Desteklenmeyen yerel grup: {group_name}")
        return sids[group_name]

    def add_user_to_group(self, username: str, group_name: str, log: Logger) -> None:
        if not username.strip():
            raise RuntimeError("Gruba eklenecek kullanici bos.")
        sid = self._group_sid(group_name)
        script = f"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop
$memberName = '{self.ps_escape(username)}'
$groupSid = [System.Security.Principal.SecurityIdentifier]::new('{sid}')
$groupName = $groupSid.Translate([System.Security.Principal.NTAccount]).Value.Split('\\')[-1]
try {{
    Add-LocalGroupMember -Group $groupName -Member $memberName -ErrorAction Stop
}} catch {{
    if ($_.Exception.Message -notmatch 'already a member|zaten.*uye') {{ throw }}
}}
$verified = Get-LocalGroupMember -Group $groupName -ErrorAction Stop |
    Where-Object {{ $_.Name -ieq $memberName -or $_.Name -ilike "*\\$memberName" }}
if ($null -eq $verified) {{ throw "Grup uyeligi dogrulanamadi: $memberName" }}
Write-Output "Group membership verified: $memberName -> $groupName"
"""
        self.run_powershell(script, log)
        log(f"{username} kullanicisinin '{group_name}' uyeligi dogrulandi.")

    def remove_user_from_group(self, username: str, group_name: str, log: Logger) -> None:
        if not username.strip():
            raise RuntimeError("Gruptan cikarilacak kullanici bos.")
        sid = self._group_sid(group_name)
        script = f"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop
$memberName = '{self.ps_escape(username)}'
$groupSid = [System.Security.Principal.SecurityIdentifier]::new('{sid}')
$groupName = $groupSid.Translate([System.Security.Principal.NTAccount]).Value.Split('\\')[-1]
$member = Get-LocalGroupMember -Group $groupName -ErrorAction Stop |
    Where-Object {{ $_.Name -ieq $memberName -or $_.Name -ilike "*\\$memberName" }} |
    Select-Object -First 1
if ($member) {{
    Remove-LocalGroupMember -Group $groupName -Member $member.Name -ErrorAction Stop
}}
$remaining = Get-LocalGroupMember -Group $groupName -ErrorAction Stop |
    Where-Object {{ $_.Name -ieq $memberName -or $_.Name -ilike "*\\$memberName" }}
if ($remaining) {{ throw "Grup uyeligi kaldirilamadi: $memberName" }}
Write-Output "Group membership removed: $memberName -> $groupName"
"""
        self.run_powershell(script, log)
        log(f"{username} kullanicisi '{group_name}' grubunda degil.")

    def set_computer_name(self, computer_name: str, log: Logger) -> None:
        if not computer_name:
            raise RuntimeError("Bilgisayar adi bos olamaz.")
        # Rename-Computer can return successfully even though the requested
        # name was not persisted.  Windows keeps the requested value in an
        # official pending-name key until reboot, so verify it before the
        # report marks this critical step as successful.
        script = f"""
$ErrorActionPreference = 'Stop'
$expected = '{self.ps_escape(computer_name)}'
$computer = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
if ($computer.PartOfDomain) {{
    # Existing domain-joined test/production devices need domain credentials
    # for a rename. Preserve their current identity; freshly formatted
    # workgroup devices continue below and are renamed normally.
    Write-Output "ACIK_COMPUTER_NAME_PRESERVED_DOMAIN: $($computer.Name)"
    exit 0
}}
$activePath = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\ComputerName\\ActiveComputerName'
$pendingPath = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\ComputerName\\ComputerName'
$activeBefore = [string](Get-ItemProperty -LiteralPath $activePath -ErrorAction Stop).ComputerName
$pendingBefore = [string](Get-ItemProperty -LiteralPath $pendingPath -ErrorAction Stop).ComputerName

if ($activeBefore -ieq $expected -and $pendingBefore -ieq $expected) {{
    Write-Output "Bilgisayar adi zaten etkin ve dogrulandi: $expected"
    exit 0
}}

Rename-Computer -NewName $expected -Force -ErrorAction Stop
$pendingAfter = [string](Get-ItemProperty -LiteralPath $pendingPath -ErrorAction Stop).ComputerName
if ([string]::IsNullOrWhiteSpace($pendingAfter) -or $pendingAfter -ine $expected) {{
    throw "Bilgisayar adi kaydi dogrulanamadi. Beklenen: $expected; kaydedilen: $pendingAfter"
}}

$activeAfter = [string](Get-ItemProperty -LiteralPath $activePath -ErrorAction Stop).ComputerName
if ($activeAfter -ieq $expected) {{
    Write-Output "Bilgisayar adi etkin olarak dogrulandi: $expected"
}} else {{
    Write-Output "Bilgisayar adi yeniden baslatma sonrasi etkinlesmek uzere dogrulandi: $expected"
}}
"""
        completed = self.run_powershell(script, log)
        if "ACIK_COMPUTER_NAME_PRESERVED_DOMAIN" in (completed.stdout or ""):
            log("Mevcut domain uyeligi nedeniyle bilgisayar adi korunarak devam edildi.")
        else:
            log(f"Bilgisayar adi hedef kayitla dogrulandi: {computer_name}")

    def _connect_existing_wifi_profile(self, ssid: str, log: Logger) -> None:
        """Connect an existing WLAN profile through the actual adapter name."""
        script = f"""
$ErrorActionPreference = 'Stop'
$profileName = '{self.ps_escape(ssid)}'
$service = Get-Service -Name WlanSvc -ErrorAction Stop
Set-Service -Name WlanSvc -StartupType Automatic
if ($service.Status -ne 'Running') {{ Start-Service -Name WlanSvc -ErrorAction Stop }}
$wifiAdapter = Get-NetAdapter -IncludeHidden -ErrorAction Stop |
    Where-Object {{ $_.InterfaceDescription -match 'Wireless|Wi-Fi|802.11' -or $_.PhysicalMediaType -match 'Native 802.11' }} |
    Select-Object -First 1
if (-not $wifiAdapter) {{ throw 'Kablosuz ag adaptoru bulunamadi.' }}
if ($wifiAdapter.Status -eq 'Disabled') {{
    Enable-NetAdapter -Name $wifiAdapter.Name -Confirm:$false -ErrorAction Stop
    Start-Sleep -Seconds 2
}}
& netsh.exe wlan connect name="$profileName" ssid="$profileName" interface="$($wifiAdapter.Name)"
if ($LASTEXITCODE -ne 0) {{ throw "Wi-Fi baglanti komutu basarisiz. Kod: $LASTEXITCODE" }}
"""
        self.run_powershell(script, log)
        for attempt in range(1, 16):
            if self.get_connected_wifi_ssid().casefold() == ssid.casefold():
                log(f"Wi-Fi baglantisi dogrulandi ({attempt}/15): {ssid}")
                return
            time.sleep(2)
        raise RuntimeError(f"Wi-Fi profili mevcut ancak baglanti dogrulanamadi: {ssid}")

    def connect_to_wifi(self, profile: WifiProfile, log: Logger) -> None:
        """Create a Wi-Fi profile and connect through the detected adapter."""
        if not profile.ssid or not profile.password:
            raise RuntimeError("Wi-Fi profili eksik.")

        ssid = profile.ssid
        password = profile.password
        xml_ssid = xml_escape(ssid)
        xml_password = xml_escape(password)
        ssid_hex = ssid.encode("utf-8").hex()
        wifi_xml = f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{xml_ssid}</name>
    <SSIDConfig>
        <SSID>
            <hex>{ssid_hex}</hex>
            <name>{xml_ssid}</name>
        </SSID>
        <nonBroadcast>false</nonBroadcast>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM>
        <security>
            <authEncryption>
                <authentication>WPA2PSK</authentication>
                <encryption>AES</encryption>
                <useOneX>false</useOneX>
            </authEncryption>
            <sharedKey>
                <keyType>passPhrase</keyType>
                <protected>false</protected>
                <keyMaterial>{xml_password}</keyMaterial>
            </sharedKey>
        </security>
    </MSM>
</WLANProfile>
"""
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".xml",
            prefix="acik_wifi_",
            delete=False,
        ) as temp_handle:
            temp_handle.write(wifi_xml)
            temp_xml = Path(temp_handle.name)
        log(f"Wi-Fi profili hazirlaniyor: {ssid}")
        try:
            script = f"""
$profileName = '{self.ps_escape(ssid)}'
& netsh.exe wlan add profile filename="{temp_xml}" user=all
if ($LASTEXITCODE -ne 0) {{ throw "Wi-Fi profili eklenemedi. Kod: $LASTEXITCODE" }}
"""
            self.run_powershell(script, log)
        finally:
            temp_xml.unlink(missing_ok=True)
        self._connect_existing_wifi_profile(ssid, log)

    def sync_time(self, log: Logger) -> None:
        log("Saat servisi esitlemesi baslatiliyor.")
        script = """
$ErrorActionPreference = 'Stop'

function Invoke-W32Tm([string[]] $Arguments) {
    & w32tm.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "w32tm $($Arguments -join ' ') basarisiz oldu. Kod: $LASTEXITCODE"
    }
}

Set-Service -Name w32time -StartupType Automatic
$svc = Get-Service -Name w32time -ErrorAction Stop
if ($svc.Status -eq 'Stopped') {
    Start-Service -Name w32time -ErrorAction Stop
}
$deadline = (Get-Date).AddSeconds(20)
do {
    $svc.Refresh()
    if ($svc.Status -eq 'Running') { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
if ($svc.Status -ne 'Running') {
    throw "Windows Time servisi calismiyor; w32tm yapilandirmasi uygulanamadi. Durum: $($svc.Status)"
}

$cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
if ($cs.PartOfDomain) {
    Invoke-W32Tm @('/config', '/syncfromflags:DOMHIER', '/update')
} else {
    Invoke-W32Tm @('/config', '/manualpeerlist:time.windows.com,0x8', '/syncfromflags:MANUAL', '/reliable:NO', '/update')
}

$lastError = ''
for ($attempt = 1; $attempt -le 3; $attempt++) {
    & w32tm.exe /resync /force | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $source = ((& w32tm.exe /query /source) | Out-String).Trim()
        if ($source -and $source -notmatch 'Local CMOS Clock|Free-running System Clock') {
            Write-Output "Time source verified: $source"
            exit 0
        }
        $lastError = "Gecerli zaman kaynagi dogrulanamadi: $source"
    } else {
        $lastError = "w32tm /resync basarisiz oldu. Kod: $LASTEXITCODE"
    }
    Start-Sleep -Seconds 4
}
throw "Saat esitlemesi uc denemede dogrulanamadi. $lastError"
"""
        self.run_powershell(script, log)
        log("Saat esitlemesi kaynak dogrulamasi ile tamamlandi.")

    def connect_wifi_and_sync_time(self, profile: WifiProfile, log: Logger) -> None:
        self.connect_to_wifi(profile, log)
        self.sync_time(log)

    def install_anydesk(self, log: Logger) -> None:
        installer = self._resolve_optional_tool_path(self.config.tools.anydesk_installer_path)
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if installer and installer.exists():
            self.verify_known_payload_integrity(installer)
            log(f"AnyDesk USB payloadi kullaniliyor: {installer}")
            source_path = installer
        else:
            if installer is not None:
                log(f"AnyDesk USB payloadi bulunamadi, internet indirimi kullanilacak: {installer}")
            else:
                log("AnyDesk payloadi ayarlanmamis. Resmi indirme kullanilacak.")
            temp_dir = tempfile.TemporaryDirectory(prefix="AcikOnboarding-AnyDesk-")
            temp_path = Path(temp_dir.name) / "AnyDesk.exe"
            log("AnyDesk indiriliyor.")
            urllib.request.urlretrieve("https://download.anydesk.com/AnyDesk.exe", temp_path)
            source_path = temp_path
        anydesk_dir = str(Path(self.config.tools.anydesk_install_dir).resolve())
        try:
            self._run(
                [
                    str(source_path),
                    "--install",
                    anydesk_dir,
                    "--silent",
                    "--create-desktop-icon",
                ],
                log,
            )
            if not self._wait_for_program("anydesk", 120, log):
                raise RuntimeError(
                    "AnyDesk yukleyicisi kapandi ancak kurulum dogrulanamadi."
                )
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
        log("AnyDesk kurulumu dosya/registry uzerinden dogrulandi.")

    def _resolve_optional_tool_path(self, raw_path: str) -> Path | None:
        normalized = raw_path.strip()
        if not normalized:
            return None
        path = Path(normalized)
        if path.is_absolute():
            return path.resolve()
        return (self.config.base_dir / path).resolve()

    def _resolve_tool_path(self, raw_path: str) -> Path:
        path = self._resolve_optional_tool_path(raw_path)
        if path is None:
            raise RuntimeError("Gerekli dosya yolu ayarlarda bos.")
        return path

    def resolve_hackbgrt_source_dir(self) -> Path:
        raw_path = self.config.tools.hackbgrt_setup_path.strip()
        if not raw_path:
            raise RuntimeError("HackBGRT yolu ayarlarda bos.")

        resolved = self._resolve_tool_path(raw_path)
        if resolved.is_file():
            resolved = resolved.parent
        if not resolved.exists() or not resolved.is_dir():
            raise RuntimeError(f"HackBGRT klasoru bulunamadi: {resolved}")
        return resolved

    def validate_hackbgrt_dir(self, directory: Path) -> None:
        required_files = [
            "setup.exe",
            "config.txt",
            "splash.bmp",
        ]
        missing = [name for name in required_files if not (directory / name).exists()]
        if missing:
            raise RuntimeError(f"HackBGRT klasoru eksik dosya iceriyor: {', '.join(missing)}")

    def hackbgrt_destination_dir(self, source_dir: Path) -> Path:
        return Path("C:/hackbgrt")

    def prepare_hackbgrt_dir(self, log: Logger) -> Path:
        source_dir = self.resolve_hackbgrt_source_dir()
        self.validate_hackbgrt_dir(source_dir)
        destination_dir = self.hackbgrt_destination_dir(source_dir)

        if source_dir.resolve() != destination_dir.resolve():
            destination_dir.parent.mkdir(parents=True, exist_ok=True)
            if destination_dir.exists():
                log(f"Eski HackBGRT hedef klasoru temizleniyor: {destination_dir}")
                shutil.rmtree(destination_dir, ignore_errors=True)
            log(f"HackBGRT paketi C surucusune kopyalaniyor: {destination_dir}")
            shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True)
        else:
            log(f"HackBGRT paketi zaten hedef klasorde: {destination_dir}")

        self.validate_hackbgrt_dir(destination_dir)
        return destination_dir

    def get_firmware_type(self, log: Logger) -> str:
        completed = self.run_powershell(
            "(Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control' -Name PEFirmwareType -ErrorAction Stop).PEFirmwareType",
            log,
            check=False,
        )
        output = completed.stdout.strip().splitlines()
        value = output[-1].strip() if output else ""
        if value == "2":
            return "UEFI"
        if value == "1":
            return "BIOS"
        return ""

    def is_secure_boot_enabled(self, log: Logger) -> bool | None:
        completed = self.run_powershell(
            """
try {
    $result = Confirm-SecureBootUEFI
    if ($result) { 'True' } else { 'False' }
} catch {
    'Unknown:' + $_.Exception.Message
}
""",
            log,
            check=False,
        )
        output = completed.stdout.strip().splitlines()
        value = output[-1].strip() if output else ""
        if value == "True":
            return True
        if value == "False":
            return False
        return None

    def ensure_hackbgrt_prerequisites(self, log: Logger) -> None:
        firmware_type = self.get_firmware_type(log)
        if firmware_type != "UEFI":
            raise RuntimeError("HackBGRT yalnizca UEFI sistemlerde kurulabilir.")

        secure_boot_enabled = self.is_secure_boot_enabled(log)
        if secure_boot_enabled:
            raise RuntimeError("HackBGRT icin Secure Boot kapali olmali.")
        if secure_boot_enabled is None:
            raise RuntimeError("Secure Boot durumu dogrulanamadi; HackBGRT guvenli bicimde baslatilmadi.")
        log("HackBGRT on kosullari dogrulandi: UEFI, Secure Boot kapali.")

    def run_eset_installer(self, log: Logger) -> None:
        installer_path = self._resolve_tool_path(self.config.tools.eset_installer_path)
        if not installer_path.exists():
            raise RuntimeError(f"ESET kurulum dosyasi bulunamadi: {installer_path}")
        if self.is_program_installed("eset"):
            log("ESET zaten kurulu; kurulum adimi atlandi.")
            return
        if not self.is_admin_session():
            raise RuntimeError("ESET kurulumu yukseltilmis yetki gerektiriyor.")

        self.verify_known_payload_integrity(installer_path)
        with tempfile.TemporaryDirectory(prefix="AcikOnboarding-ESET-") as temp_dir:
            temp_installer = Path(temp_dir) / installer_path.name
            shutil.copy2(installer_path, temp_installer)
            source_hash = hashlib.sha256(installer_path.read_bytes()).digest()
            copied_hash = hashlib.sha256(temp_installer.read_bytes()).digest()
            if source_hash != copied_hash:
                raise RuntimeError("ESET gecici kopyasinin SHA-256 dogrulamasi basarisiz.")
            log(f"ESET kurulumu dogrulanmis gecici kopyadan baslatiliyor: {temp_installer}")
            completed = self._run(
                [str(temp_installer), "/qn", "/norestart", "--silent"],
                log,
                check=False,
            )
        if completed.returncode not in {0, 1641, 3010}:
            raise RuntimeError(f"ESET yukleyicisi hata kodu dondurdu: {completed.returncode}")
        if completed.returncode in {1641, 3010}:
            log("ESET kurulumu basarili; yeniden baslatma gerekiyor.")
        else:
            log("ESET yukleyicisi basariyla tamamlandi.")

    def is_windows_activated(self, log: Logger) -> bool:
        completed = self.run_powershell(
            "(Get-CimInstance -query 'select LicenseStatus from SoftwareLicensingProduct where PartialProductKey is not null').LicenseStatus",
            log,
            check=False,
        )
        return completed.stdout.strip() == "1"

    def activate_windows(self, log: Logger) -> None:
        product_key = self.config.windows.activation_product_key.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}", product_key):
            raise RuntimeError("Windows urun anahtari bicimi gecersiz.")

        log("Windows lisans anahtari uygulanmaya calisiliyor.")
        script = f"""
$productKey = '{self.ps_escape(product_key)}'
& cscript.exe //NoLogo "$env:SystemRoot\\System32\\slmgr.vbs" /ipk $productKey
if ($LASTEXITCODE -ne 0) {{ throw "Windows urun anahtari uygulanamadi: $LASTEXITCODE" }}
Start-Sleep -Seconds 3
& cscript.exe //NoLogo "$env:SystemRoot\\System32\\slmgr.vbs" /ato
if ($LASTEXITCODE -ne 0) {{ throw "Windows etkinlestirme basarisiz: $LASTEXITCODE" }}
Start-Sleep -Seconds 2
& cscript.exe //NoLogo "$env:SystemRoot\\System32\\slmgr.vbs" /xpr
"""
        self.run_powershell(script, log)
        log("Windows etkinlestirme adimlari tamamlandi.")

    def check_windows_activation(self, log: Logger) -> None:
        if self.is_windows_activated(log):
            log("Windows zaten etkin durumda.")
            return
        self.activate_windows(log)

    def open_windows_update_page(self, log: Logger) -> None:
        uri = self.config.windows.update_uri.strip() or "ms-settings:windowsupdate"
        log("Windows Update sayfasi aciliyor.")
        self.run_powershell(f"Start-Process '{self.ps_escape(uri)}'", log)
        log("Windows Update sayfasi kullaniciya acildi.")

    def join_domain(
        self,
        computer_name: str,
        target_username: str,
        target_password: str,
        log: Logger,
    ) -> None:
        domain = self.config.domain
        if not domain.name or not domain.username or not domain.password:
            raise RuntimeError("Domain ayarlari eksik.")
        # Cihaz domain'e bu hedef kullanicinin parolasi ile degil, asagida
        # olusturulan yetkili domain kimligiyle alinir. Hedef kullanici yeni
        # olabilir veya farkli bir oturum ilkesi kullanabilir; bu nedenle onun
        # dogrulamasi bilgisayarin domain katilimini engellememelidir.
        # Parametreler workflow API uyumlulugu icin korunur.
        _ = target_username, target_password

        wifi_profile = self.config.wifi_profiles.get("domain_join")
        if wifi_profile and wifi_profile.ssid:
            log(f"Domain oncesi kurumsal Wi-Fi baglantisi deneniyor: {wifi_profile.ssid}")
            self.connect_wifi_and_sync_time(wifi_profile, log)

        log(f"Cihaz domaine alinmaya calisiliyor: {domain.name}")
        script = f"""
$ErrorActionPreference = 'Stop'
$domain = '{self.ps_escape(domain.name)}'
$username = '{self.ps_escape(domain.username)}'
if ($username -notmatch '\\\\|@') {{
    $username = "$domain\\$username"
}}
$password = '{self.ps_escape(domain.password)}'
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($username, $securePassword)
$computer = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
$currentDomain = [string]$computer.Domain
if ($computer.PartOfDomain -and $currentDomain.Trim().TrimEnd('.') -ieq $domain.Trim().TrimEnd('.')) {{
    # A rerun on a provisioned test device must not alter its existing domain
    # membership. Add-Computer rejects this state, and secure-channel repair
    # could change the test machine's domain account. Clean devices are not
    # members yet and continue through the Add-Computer path below.
    Write-Output "ACIK_DOMAIN_ALREADY_JOINED: $currentDomain"
    exit 0
}}
Add-Computer -DomainName $domain -Credential $credential -NewName '{self.ps_escape(computer_name)}' -Force -ErrorAction Stop
Write-Output "ACIK_DOMAIN_JOIN_REQUESTED: $domain"
"""
        completed = self.run_powershell(script, log, extra_secrets=(domain.password,))
        if "ACIK_DOMAIN_ALREADY_JOINED" in (completed.stdout or ""):
            log("Cihaz zaten hedef domaine uye; mevcut domain uyeligine dokunulmadan devam edilecek.")
        else:
            log("Yetkili domain kimligiyle domain katilim komutu basariyla gonderildi. Yeniden baslatma gerekecek.")

    def leave_domain(self, credential_username: str, log: Logger) -> str:
        """Safely request an explicit domain unjoin and restart the device.

        This is an operator recovery action for a test device that was left
        domain-joined.  It is never part of the normal onboarding pipeline and
        does not alter a pending X-cleanup workflow.  The operator may enter a
        different approved domain username, but the password always remains in
        the protected application configuration and is never displayed or
        written to logs.
        """
        if not self.is_admin_session():
            raise RuntimeError("Domainden cikis icin yonetici yetkisi gerekli.")
        domain = self.config.domain
        if not domain.name.strip() or not domain.password:
            raise RuntimeError("Domainden cikis icin kayitli domain adi ve sifresi gerekli.")
        raw_username = credential_username.strip() or domain.username.strip()
        if not raw_username:
            raise RuntimeError("Domainden cikis icin domain kullanici adi gerekli.")
        if not re.fullmatch(r"[A-Za-z0-9._@\\-]{1,128}", raw_username):
            raise RuntimeError("Domainden cikis kullanici adi gecersiz karakter iceriyor.")

        script = f"""
$ErrorActionPreference = 'Stop'
$configuredDomain = '{self.ps_escape(domain.name.strip())}'
$username = '{self.ps_escape(raw_username)}'
if ($username -notmatch '\\\\|@') {{
    $username = "$configuredDomain\\$username"
}}
$password = '{self.ps_escape(domain.password)}'
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($username, $securePassword)
$computer = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
if (-not $computer.PartOfDomain) {{
    Write-Output 'ACIK_NOT_DOMAIN_JOINED'
    exit 0
}}
$joinedDomain = [string]$computer.Domain
Remove-Computer -UnjoinDomainCredential $credential -WorkgroupName 'WORKGROUP' -Force -PassThru -ErrorAction Stop | Out-Null
Write-Output "ACIK_DOMAIN_UNJOIN_REQUESTED: $joinedDomain"
"""
        completed = self.run_powershell(script, log, extra_secrets=(domain.password,))
        output = completed.stdout or ""
        if "ACIK_NOT_DOMAIN_JOINED" in output:
            log("Cihaz zaten bir domaine uye degil; domainden cikis ve yeniden baslatma atlandi.")
            return ""
        match = re.search(r"ACIK_DOMAIN_UNJOIN_REQUESTED:\s*(.+)", output)
        if not match:
            raise RuntimeError("Domainden cikis komutu dogrulanamadi.")
        previous_domain = match.group(1).strip()

        # Restore a normal local account chooser before the reboot.  This does
        # not cancel or modify an existing onboarding state; it only makes the
        # local recovery account accessible after the workgroup transition.
        self.restore_local_account_picker(log)
        self._run(["shutdown.exe", "/r", "/t", "15", "/f"], log)
        log(
            "Domainden cikis basariyla planlandi; cihaz 15 saniye icinde yeniden "
            "baslatilacak. Kayitli parola ekranda veya gunlukte gosterilmedi."
        )
        return previous_domain

    def run_hackbgrt(self, log: Logger) -> None:
        """Install HackBGRT through its documented non-interactive batch mode."""
        self.ensure_hackbgrt_prerequisites(log)
        hackbgrt_dir = self.prepare_hackbgrt_dir(log)
        setup_exe = hackbgrt_dir / "setup.exe"
        setup_log = hackbgrt_dir / "setup.log"
        if not setup_exe.exists():
            raise RuntimeError(f"HackBGRT setup.exe bulunamadi: {setup_exe}")

        previous_log_size = setup_log.stat().st_size if setup_log.exists() else 0
        log("HackBGRT batch kurulumu baslatiliyor: install + enable-entry")
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        try:
            completed = subprocess.run(
                [str(setup_exe), "batch", "install", "enable-entry"],
                cwd=str(hackbgrt_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("HackBGRT kurulumu 120 saniyede tamamlanmadi.") from exc
        if completed.stdout.strip():
            log(completed.stdout.strip())
        if completed.stderr.strip():
            log(completed.stderr.strip())
        if completed.returncode != 0:
            raise RuntimeError(f"HackBGRT kurulum komutu basarisiz oldu (kod {completed.returncode}).")

        appended_log = ""
        if setup_log.exists():
            with setup_log.open("rb") as handle:
                handle.seek(min(previous_log_size, setup_log.stat().st_size))
                appended_log = handle.read().decode("utf-8", errors="replace")
        required_markers = (
            "Completed action 'install' successfully.",
            "Completed action 'enable-entry' successfully.",
        )
        if not all(marker in appended_log for marker in required_markers):
            raise RuntimeError("HackBGRT setup.exe kapandi ancak install/enable-entry sonucu dogrulanamadi.")
        log("HackBGRT kurulumu ve EFI boot girdisi setup.log uzerinden dogrulandi.")

    def clear_post_login_state(self) -> None:
        self.post_login_state_path().unlink(missing_ok=True)

    def clear_post_login_helper(self) -> None:
        self.startup_helper_path().unlink(missing_ok=True)

    def clear_user_phase_artifacts(self, run_id: str) -> None:
        target = self.user_phase_dir(run_id).resolve()
        root = (self.runtime_dir() / "user").resolve()
        if target.parent != root:
            raise RuntimeError("Kullanici fazi temizleme hedefi guvenli kokun disinda.")
        if target.exists():
            shutil.rmtree(target)

    def connect_main_file_server(self, host: str, share: str, username: str, password: str, shortcut_name: str, log: Logger) -> None:
        unc_path = self.build_unc_path(host, share)
        log(f"Ana File Server baglantisi kuruluyor: {unc_path}")
        self.connect_network_resource(unc_path, username, password, log)

        # Kisayolu her kosulda olusturuyoruz (offline bile olsa masaustunde kisayol bulunmali)
        try:
            # Kullanıcı talebi doğrultusunda kısayol doğrudan IP kök dizinine (\\10.9.10.174) oluşturulur.
            s_name = shortcut_name.strip() if shortcut_name else "File Server"
            if s_name == "FileServer":
                s_name = "File Server"
            self.create_desktop_shortcut(unc_path, s_name, log)
        except Exception as exc:
            raise RuntimeError(f"Masaustu kisayolu olusturulamadi: {exc}") from exc
        log("Ana File Server kisayol adimi tamamlandi.")

    def connect_network_printer(self, host: str, share: str, username: str, password: str, log: Logger) -> None:
        unc_path = self.build_unc_path(host, share)
        log(f"Ag yazicisi baglantisi kuruluyor: {unc_path}")
        self.connect_network_resource(unc_path, username, password, log, resource_type=2)
        script = f"""
$printerPath = '{self.ps_escape(unc_path)}'
try {{
    $net = New-Object -ComObject WScript.Network
    $net.AddWindowsPrinterConnection($printerPath)
    Write-Output "WScript.Network ile yazici baglantisi basarili."
}} catch {{
    Add-Printer -ConnectionName $printerPath -ErrorAction Stop
    Write-Output "Add-Printer ile yazici baglantisi basarili."
}}
"""
        self.run_powershell(script, log)
        # Masaüstünde yazıcı için kısayol oluştur
        try:
            self.create_desktop_shortcut(unc_path, "Ağ Yazıcısı", log)
            log("Masaustunde Ag Yazicisi kisayolu olusturuldu.")
        except Exception as exc:
            log(f"Masaustunde Ag Yazicisi kisayolu olusturulamadi: {exc}")

    def post_login_bundle_dir(self) -> Path:
        program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
        return program_data / "AcikOnboarding" / "app"

    def _bundle_ignore(self, _directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        blocked_dirs = {
            ".venv",
            ".dev-venv",
            ".build-venv",
            "build",
            "build-v3",
            "build-v4",
            "build-v5",
            "build-v6",
            "build-v7",
            "dist",
            "release",
            "runtime",
            "__pycache__",
            ".pytest_cache",
        }
        blocked_files = {"app_config.local.json", "JoinDomain.ps1"}
        for name in names:
            lowered = name.lower()
            if name in blocked_dirs or name in blocked_files or lowered.endswith(".pyc"):
                ignored.add(name)
            elif lowered.endswith(".txt") and "key" in lowered:
                ignored.add(name)
        return ignored

    def protect_post_login_bundle(self, target: Path) -> None:
        """Prevent a standard user from replacing code later run as SYSTEM."""
        if sys.platform != "win32":
            return
        if not self.is_admin_session():
            raise RuntimeError("Ikinci faz uygulama paketi ACL'i icin yonetici yetkisi gerekli.")
        result = self._run_quiet(
            [
                "icacls.exe",
                str(target),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)(F)",
                "*S-1-5-32-544:(OI)(CI)(F)",
                "*S-1-5-32-545:(OI)(CI)(RX)",
                "/t",
                "/c",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("Ikinci faz uygulama paketi guvenli ACL ile korunamadi.")

    def deploy_post_login_bundle(self, log: Logger) -> Path:
        source = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else self.config.base_dir.resolve()
        target = self.post_login_bundle_dir()
        staging = target.with_name(f"{target.name}.staging.{os.getpid()}")
        root = target.parent.resolve()
        if target.resolve().parent != root or staging.resolve().parent != root:
            raise RuntimeError("Post-login hedef klasoru guvenli kokun disinda.")
        if staging.exists():
            shutil.rmtree(staging)
        staging.parent.mkdir(parents=True, exist_ok=True)
        log(f"Ikinci faz uygulama paketi yerel diske kopyalaniyor: {target}")
        try:
            shutil.copytree(source, staging, ignore=self._bundle_ignore)
            if getattr(sys, "frozen", False):
                expected_entry = staging / Path(sys.executable).name
            else:
                expected_entry = staging / "run_app.py"
            if not expected_entry.exists():
                raise RuntimeError(f"Kopyalanan pakette giris dosyasi bulunamadi: {expected_entry.name}")
            self.protect_post_login_bundle(staging)
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        log("Ikinci faz uygulama paketi atomik olarak hazirlandi.")
        return target

    def _bundle_relative_path(self, raw_path: str, bundle_dir: Path) -> str:
        if not raw_path.strip():
            return ""
        path = Path(raw_path)
        if path.is_absolute():
            try:
                relative = path.resolve().relative_to(self.config.base_dir.resolve())
            except ValueError:
                return str(path)
            return str(bundle_dir / relative)
        return str(bundle_dir / path)

    def build_post_login_launch_command(
        self,
        run_id: str,
        target_username: str,
        *,
        wait_for_exit: bool = False,
    ) -> str:
        bundle_dir = self.post_login_bundle_dir()
        wait_flag = "/wait " if wait_for_exit else ""
        if getattr(sys, "frozen", False):
            target_exe = bundle_dir / Path(sys.executable).name
            return (
                f'start "" {wait_flag}"{target_exe}" --post-login '
                f'"{run_id}" "{target_username}"'
            )
        target_py = bundle_dir / "run_app.py"
        return (
            f'start "" {wait_flag}"{self.post_login_python_path()}" '
            f'"{target_py}" --post-login "{run_id}" "{target_username}"'
        )

    def install_post_login_startup_helper(
        self,
        run_id: str,
        target_username: str,
        log: Logger,
    ) -> None:
        """Install a safe fallback for the target user's first logon.

        Task Scheduler is the primary launch mechanism.  Some OEM images do
        not start a SID-bound interactive task on the very first local-account
        sign-in, though.  The common Startup entry is therefore a small,
        target-name-guarded fallback.  It contains no credential or protected
        workflow state and simply starts the existing standard-user phase.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            raise RuntimeError("Post-login baslangic yardimcisi icin run_id gecersiz.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", target_username):
            raise RuntimeError("Post-login baslangic yardimcisi icin hedef kullanici gecersiz.")

        helper = self.startup_helper_path()
        helper.parent.mkdir(parents=True, exist_ok=True)
        launch = self.build_post_login_launch_command(run_id, target_username)
        content = "\r\n".join(
            (
                "@echo off",
                "setlocal",
                f'if /I not "%USERNAME%"=="{target_username}" exit /b 0',
                "set \"ACIK_SKIP_ELEVATION=1\"",
                launch,
                "endlocal",
                "",
            )
        )
        temporary = helper.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="")
            os.replace(temporary, helper)
        finally:
            temporary.unlink(missing_ok=True)
        if not helper.is_file():
            raise RuntimeError("Post-login baslangic yardimcisi yazilamadi.")
        log("Ilk hedef oturumunda ikinci fazin otomatik baslamasi icin Startup yedegi hazirlandi.")

    def _user_phase_command(self, run_id: str, target_username: str) -> tuple[str, str, str]:
        """Build the standard-user post-login task command without a cmd file."""
        bundle_dir = self.post_login_bundle_dir()
        if getattr(sys, "frozen", False):
            executable = bundle_dir / Path(sys.executable).name
            arguments = subprocess.list2cmdline(["--post-login", run_id, target_username])
        else:
            executable = self.post_login_python_path()
            script = bundle_dir / "run_app.py"
            arguments = subprocess.list2cmdline([str(script), "--post-login", run_id, target_username])
        return str(executable), arguments, str(bundle_dir)

    def install_user_phase_task(
        self,
        run_id: str,
        target_principal: str,
        target_username: str,
        log: Logger,
    ) -> str:
        """Run user-scoped work once at the target account's next logon.

        This replaces the common Startup ``.cmd`` helper.  It needs no target
        password, no UAC prompt, and no temporary Administrators membership.
        Privileged work remains in the separate SYSTEM finalizer task.
        """
        if not target_principal.strip() or not target_username.strip():
            raise RuntimeError("Kullanici fazi gorevi icin hedef hesap eksik.")
        task_name = f"AcikOnboardingUserPhase-{run_id[:12]}"
        executable, arguments, working_dir = self._user_phase_command(run_id, target_username)
        script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{self.ps_escape(executable)}' -Argument '{self.ps_escape(arguments)}' -WorkingDirectory '{self.ps_escape(working_dir)}'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User '{self.ps_escape(target_principal)}'
$principal = New-ScheduledTaskPrincipal -UserId '{self.ps_escape(target_principal)}' -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if (-not (Get-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -ErrorAction SilentlyContinue)) {{
    throw 'Kullanici fazi gorevi dogrulanamadi.'
}}
"""
        self.run_powershell(script, log)
        log(f"Hedef kullanici icin UAC istemeyen ikinci faz gorevi hazirlandi: {task_name}")
        return task_name

    def cancel_scheduled_task_quickly(self, task_name: str, log: Logger) -> None:
        """End and unregister one AÇIK task without waiting for its action.

        ``Stop-ScheduledTask`` can wait indefinitely when a child task is in
        ``query user`` or a damaged Windows session.  ``schtasks /End`` sends
        the stop request immediately; each call has a short hard timeout so
        the recovery card can never freeze the UI worker.
        """
        if not task_name or not re.fullmatch(r"AcikOnboarding[A-Za-z0-9_-]{1,96}", task_name):
            return
        end_result = self._run_quiet(
            ["schtasks.exe", "/End", "/TN", task_name],
            timeout_seconds=5,
        )
        delete_result = self._run_quiet(
            ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
            timeout_seconds=5,
        )
        if end_result.returncode == 124 or delete_result.returncode == 124:
            log(f"Planlanan gorev zaman asimina ugradi ancak UI bekletilmedi: {task_name}")
        else:
            log(f"Planlanan gorev sonlandirildi ve kaydi kaldirildi: {task_name}")

    def remove_user_phase_task(self, task_name: str, log: Logger) -> None:
        self.cancel_scheduled_task_quickly(task_name, log)

    def _system_finalize_command(self, run_id: str) -> tuple[str, str, str]:
        bundle_dir = self.post_login_bundle_dir()
        if getattr(sys, "frozen", False):
            executable = bundle_dir / Path(sys.executable).name
            arguments = subprocess.list2cmdline(["--system-finalize", run_id])
        else:
            executable = self.post_login_python_path()
            script = bundle_dir / "run_app.py"
            arguments = subprocess.list2cmdline([str(script), "--system-finalize", run_id])
        return str(executable), arguments, str(bundle_dir)

    def install_system_finalize_task(
        self,
        run_id: str,
        target_identity: str,
        log: Logger,
        *,
        all_user_logons: bool = False,
    ) -> str:
        if not target_identity.strip():
            raise RuntimeError("SYSTEM finalizasyon gorevi icin hedef kullanici eksik.")
        task_name = f"AcikOnboardingFinalize-{run_id[:12]}"
        executable, arguments, working_dir = self._system_finalize_command(run_id)
        logon_trigger = (
            "$logonTrigger = New-ScheduledTaskTrigger -AtLogOn"
            if all_user_logons
            else (
                "$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "
                f"'{self.ps_escape(target_identity)}'"
            )
        )
        script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{self.ps_escape(executable)}' -Argument '{self.ps_escape(arguments)}' -WorkingDirectory '{self.ps_escape(working_dir)}'
{logon_trigger}
# The finalizer must survive a manual reboot and must not race the user phase.
# It wakes at the target account's logon and then polls every two minutes.  A
# poll made while a different account is active is a harmless no-op, except
# that an unexpectedly auto-signed-in X session is logged off so Windows
# returns to the account picker. The destructive X cleanup is never attempted
# until the intended account has signed in.
$retryTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Hours 47)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -Action $action -Trigger @($logonTrigger, $retryTrigger) -Principal $principal -Settings $settings -Force | Out-Null
if (-not (Get-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -ErrorAction SilentlyContinue)) {{
    throw 'SYSTEM finalizasyon gorevi dogrulanamadi.'
}}
"""
        self.run_powershell(script, log)
        if all_user_logons:
            log(
                "SYSTEM finalizasyon gorevi domain guven iliskisi yeniden "
                f"baslatmadan sonra etkinlesecek sekilde hazirlandi: {task_name}"
            )
        else:
            log(f"SYSTEM finalizasyon gorevi yalnizca hedef kullanici oturumunda calisacak sekilde hazirlandi: {task_name}")
        return task_name

    def remove_system_finalize_task(self, task_name: str, log: Logger) -> None:
        self.cancel_scheduled_task_quickly(task_name, log)

    def remove_initial_restart_task(self, task_name: str, log: Logger) -> None:
        self.cancel_scheduled_task_quickly(task_name, log)

    def remove_orphaned_onboarding_tasks(self, log: Logger, run_id: str = "") -> None:
        """Remove stopped/running tasks belonging only to the cancelled run."""
        suffix = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:12]
        patterns = (
            [
                f"AcikOnboardingUserPhase-{suffix}*",
                f"AcikOnboardingFinalize-{suffix}*",
                f"AcikOnboardingInitialRestart-{suffix}*",
                f"AcikOnboardingXCleanup-{suffix}*",
            ]
            if suffix
            else [
                "AcikOnboardingUserPhase-*",
                "AcikOnboardingFinalize-*",
                "AcikOnboardingInitialRestart-*",
                "AcikOnboardingXCleanup-*",
            ]
        )
        pattern_lines = ", ".join(f"'{self.ps_escape(pattern)}'" for pattern in patterns)
        script = f"""
$ErrorActionPreference = 'Continue'
$patterns = @({pattern_lines})
foreach ($pattern in $patterns) {{
    foreach ($task in @(Get-ScheduledTask -TaskName $pattern -ErrorAction SilentlyContinue)) {{
        # Do not use Stop-ScheduledTask here: it may wait forever for a
        # child ``query user`` process. schtasks /End is a non-blocking stop
        # request; the outer PowerShell has an eight-second deadline below.
        & schtasks.exe /End /TN $task.TaskName 2>$null | Out-Null
        & schtasks.exe /Delete /TN $task.TaskName /F 2>$null | Out-Null
    }}
}}
"""
        self.run_powershell(script, log, check=False, timeout_seconds=8)

    def remove_legacy_startup_finalize_tasks(self, log: Logger) -> None:
        """Remove obsolete boot and immediate-X tasks left by old releases.

        Earlier packages registered the SYSTEM finalizer with an ``AtStartup``
        trigger.  It can run before the target user has signed in and create
        a background error.  Current ``AtLogOn`` tasks are deliberately left
        intact; they belong to a valid active workflow.  Older X-cleanup
        tasks are also removed: those attempted to delete the active initial
        account before the target user had logged on.
        """
        script = """
$ErrorActionPreference = 'Stop'
$tasks = @(Get-ScheduledTask -TaskName 'AcikOnboardingFinalize-*' -ErrorAction SilentlyContinue)
$removed = 0
foreach ($task in $tasks) {
    $isBootTrigger = @($task.Triggers | Where-Object {
        $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger'
    }).Count -gt 0
    if ($isBootTrigger) {
        Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
        $removed++
    }
}
$legacyXTasks = @(Get-ScheduledTask -TaskName 'AcikOnboardingXCleanup-*' -ErrorAction SilentlyContinue)
foreach ($task in $legacyXTasks) {
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
    $removed++
}
Write-Output "Legacy boot/X finalizers removed: $removed"
"""
        self.run_powershell(script, log, check=False)
        log("Eski AtStartup AÇIK finalizasyon görevleri denetlendi ve varsa temizlendi.")

    def schedule_post_login_tasks(
        self,
        request: OnboardingRequest,
        log: Logger,
        *,
        immediate_x_cleanup: bool = False,
    ) -> None:
        self.validate_request(request)
        self.assert_no_active_workflow(log)
        self.remove_legacy_startup_finalize_tasks(log)
        self.clear_post_login_helper()
        bundle_dir = self.deploy_post_login_bundle(log)
        options = dict(request.options)
        resource = self.config.network_resources
        desktop = self.config.desktop_automation

        # A fixed desktop background is intentionally limited to the local,
        # standard account created by this installer. It must never become a
        # device-wide policy (which would also affect domain users), nor be
        # applied to a local account deliberately made an administrator.
        local_standard_wallpaper = bool(
            request.user_type == "Lokal"
            and options.get("desktop_wallpaper")
            and not options.get("administrator")
        )

        enabled_user: list[str] = []
        if options.get("main_file_server") or options.get("network_printer"):
            enabled_user.append("wifi_ready")
        for option_name in (
            "main_file_server",
            "network_printer",
            "desktop_signature",
            "classic_outlook",
            "windows_update",
        ):
            if options.get(option_name):
                enabled_user.append(option_name)
        if local_standard_wallpaper:
            enabled_user.append("desktop_wallpaper")

        enabled_system: list[str] = []
        if options.get("eset"):
            enabled_system.append("eset")
        if options.get("lock_screen"):
            # Device-wide policy (Enterprise/Education Group Policy, or the
            # Personalization CSP where a managed device supports it). Unlike
            # local_wallpaper_lock this is not scoped to the target user's SID,
            # so it does not depend on local_standard_wallpaper.
            enabled_system.append("lock_screen")
        if local_standard_wallpaper:
            # The user phase applies the image in the interactive session.
            # SYSTEM then locks the documented per-user policy only after the
            # target SID hive is loaded.
            enabled_system.append("local_wallpaper_lock")
        if request.user_type == "Domain" and options.get("ip_admin"):
            enabled_system.append("grant_ip_admin")
        if request.user_type == "Domain" and options.get("administrator"):
            enabled_system.append("grant_administrator")
        if options.get("delete_x_user"):
            # Keep a durable task result in the protected state even when the
            # first SYSTEM cleanup runs immediately after the initial report.
            # The target-login finalizer will make the removal outcome visible
            # in the report without treating a scheduled cleanup as deleted.
            enabled_system.append("delete_x_user")
        if not enabled_user and not enabled_system:
            return
        if options.get("main_file_server") and (
            not resource.file_server_host.strip() or not resource.file_server_share.strip()
        ):
            raise RuntimeError("Ana File Server ayarlari eksik.")
        if options.get("network_printer") and (
            not resource.printer_host.strip() or not resource.printer_share.strip()
        ):
            raise RuntimeError("Ag yazicisi ayarlari eksik.")

        wallpaper_target = ""
        if local_standard_wallpaper:
            # Validate the portable D:/USB source now, then use the immutable
            # post-login bundle copy after reboot. This never falls back to a
            # stale legacy C:\\ACIK.3 path or a USB drive letter later on.
            self.prepare_wallpaper_asset(log)
            staged_wallpaper = bundle_dir / "assets" / "wallpaper.jpg"
            if not staged_wallpaper.is_file():
                raise RuntimeError("Ikinci faz paketinde duvar kagidi gorseli bulunamadi.")
            wallpaper_target = str(staged_wallpaper)
            log(
                "Yerel standart kullanici icin sabit duvar kagidi USB "
                f"kaynagindan dogrulandi: {staged_wallpaper}"
            )

        desktop_state: dict[str, object] = {
            "signature_source_dir": self._bundle_relative_path(str(desktop.signature_source_dir), bundle_dir),
            "signature_folder_name": desktop.signature_folder_name.strip() or "Imza",
            "outlook_classic_path": desktop.outlook_classic_path.strip(),
        }
        if local_standard_wallpaper:
            desktop_state.update(
                {
                    "wallpaper_target_path": wallpaper_target,
                    # The interactive user only sets the image. The immutable
                    # policy is applied by SYSTEM after this profile loads.
                    "wallpaper_lock_change": False,
                    "wallpaper_policy_scope": "local_standard_user",
                }
            )

        run_id = request.run_id or uuid.uuid4().hex
        request.run_id = run_id
        target_principal = self._target_principal(request.username, request.user_type)
        target_sid = self.resolve_account_sid(target_principal, log)
        if request.user_type == "Lokal" and not target_sid:
            # Do not hand ``.\\user`` to Task Scheduler: on some elevated
            # sessions it cannot translate that shorthand.  The local account
            # was created in the first phase, but verify it one final time at
            # the hand-off boundary and repair an interrupted creation safely.
            log("Hedef yerel kullanici SID'i yeniden dogrulaniyor.")
            self.create_or_update_local_user(
                request.full_name,
                request.username,
                request.password,
                log,
            )
            target_sid = self.resolve_account_sid(target_principal, log)
            if not target_sid:
                raise RuntimeError(
                    "Hedef yerel kullanici olusturuldu ancak SID'i dogrulanamadi; "
                    "ikinci faz gorevi guvenle planlanmadi."
                )
        # A SID survives a computer rename and is the unambiguous Task
        # Scheduler identity for local accounts. Domain accounts fall back to
        # their principal when the SID is not materialised until first logon.
        target_task_identity = target_sid or target_principal
        state: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "request_fingerprint": self.request_fingerprint(request),
            "created_at": utc_now(),
            "expires_at": (datetime.now().astimezone() + timedelta(days=2)).isoformat(
                timespec="seconds"
            ),
            "updated_at": utc_now(),
            "target_username": request.username,
            "target_user_type": request.user_type,
            "target_principal": target_principal,
            "target_sid": target_sid,
            "company_name": request.company_name,
            "profile_name": request.profile_name,
            "computer_name": request.computer_name,
            "options": options,
            "credential_username": self.build_network_username(request.username),
            "credential_password_protected": self.protect_secret(request.password),
            "required_wifi_ssid": resource.required_wifi_ssid.strip(),
            "tasks": make_task_map([*enabled_user, *enabled_system]),
            "phases": {
                "user": {"status": "pending", "last_error": ""},
                "system": {"status": "pending", "last_error": ""},
            },
            "file_server": {
                "host": resource.file_server_host.strip(),
                "share": resource.file_server_share.strip(),
                "shortcut_name": resource.file_server_shortcut_name.strip() or "File Server",
            },
            "printer": {
                "host": resource.printer_host.strip(),
                "share": resource.printer_share.strip(),
            },
            "desktop_automation": desktop_state,
            "eset_installer_path": self._bundle_relative_path(self.config.tools.eset_installer_path, bundle_dir),
            "local_admin_username": self.config.tools.local_admin_username.strip(),
            "legacy_cleanup_user": self.config.legacy_cleanup_user.strip(),
            "immediate_x_cleanup": immediate_x_cleanup,
            "report_path": str(self.report_output_dir() / f"{run_id}.json"),
            "user_task_name": "",
            "system_task_name": "",
        }

        user_task_name = ""
        system_task_name = ""
        try:
            self.write_workflow_state(state, log)
            if enabled_user:
                self.write_user_phase_plan(state, log)
                if request.user_type == "Domain":
                    # Add-Computer does not activate the workstation trust
                    # until the restart. Registering an interactive task as
                    # DOMAIN\\user here fails with 0x800706FD.
                    log(
                        "Domain kullanici gorevi yeniden baslatma sonrasina "
                        "ertelendi; hedef oturumda Startup yardimcisi calisacak."
                    )
                else:
                    user_task_name = self.install_user_phase_task(
                        run_id,
                        target_task_identity,
                        request.username,
                        log,
                    )
                    state["user_task_name"] = user_task_name
                    self.write_workflow_state(state, log)
                self.install_post_login_startup_helper(run_id, request.username, log)
            # Even a user-only phase needs a short SYSTEM reconciliation pass
            # so its allowlisted result can be merged into the protected report.
            if enabled_user or enabled_system:
                if request.user_type == "Domain":
                    # An unqualified SYSTEM logon trigger survives the first
                    # restart without asking Task Scheduler to resolve a
                    # domain identity before its trust is active.
                    system_task_name = self.install_system_finalize_task(
                        run_id,
                        target_task_identity,
                        log,
                        all_user_logons=True,
                    )
                else:
                    system_task_name = self.install_system_finalize_task(
                        run_id,
                        target_task_identity,
                        log,
                    )
                state["system_task_name"] = system_task_name
                self.write_workflow_state(state, log)
        except Exception:
            # Also remove an old helper if an interrupted v5.3.8 run left it.
            self.clear_post_login_helper()
            if user_task_name:
                self.remove_user_phase_task(user_task_name, log)
            if system_task_name:
                self.remove_system_finalize_task(system_task_name, log)
            raise
        log(f"Ikinci faz is akisi planlandi. Run ID: {run_id}")

    def _run_state_task(
        self,
        state: dict[str, object],
        task_name: str,
        callback: Callable[[], None],
        success_message: str,
        log: Logger,
        messages: list[UiMessage],
        state_writer: Callable[[dict[str, object], Logger | None], None] | None = None,
    ) -> None:
        writer = state_writer or self.write_workflow_state
        if task_name not in unfinished_phase_tasks(
            state,
            "user" if task_name in USER_PHASE_TASKS else "system",
        ):
            return
        mark_task(state, task_name, TASK_RUNNING)
        writer(state, log)
        try:
            callback()
        except Exception as exc:
            tasks = state.get("tasks", {})
            task = tasks.get(task_name, {}) if isinstance(tasks, dict) else {}
            attempts = int(task.get("attempts", 0)) if isinstance(task, dict) else 1
            # X cleanup is the penultimate, destructive phase.  A transient
            # profile lock must never turn it into a terminal failure: leave
            # the durable SYSTEM task in place so a later automatic/manual
            # restart can retry it before the final reboot is requested.
            status = (
                TASK_RETRYABLE_FAILED
                if task_name == "delete_x_user"
                else (TASK_PERMANENT_FAILED if attempts >= 3 else TASK_RETRYABLE_FAILED)
            )
            mark_task(state, task_name, status, str(exc))
            writer(state, log)
            messages.append(("Hata", f"{task_name}: {exc}", "error"))
            log(f"Gorev basarisiz ({attempts}/3): {task_name} - {exc}")
            return
        mark_task(state, task_name, TASK_SUCCEEDED)
        writer(state, log)
        messages.append(("Bilgi", success_message, "info"))
        log(f"Gorev dogrulandi: {task_name}")

    def _deploy_signature_from_state(self, state: dict[str, object], log: Logger) -> None:
        settings = state.get("desktop_automation", {})
        if not isinstance(settings, dict):
            raise RuntimeError("Imza ayarlari gecersiz.")
        source = Path(str(settings.get("signature_source_dir", "")))
        if not source.exists() or not source.is_dir():
            raise RuntimeError(f"Imza kaynagi bulunamadi: {source}")
        target = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        target = target / (str(settings.get("signature_folder_name", "Imza")).strip() or "Imza")
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            destination = target / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(item, destination)
        if not any(target.iterdir()):
            raise RuntimeError("Imza klasoru olusturuldu ancak dosya kopyalanmadi.")
        log(f"Imza dosyalari dogrulandi: {target}")

    def _launch_outlook_from_state(self, state: dict[str, object], log: Logger) -> None:
        settings = state.get("desktop_automation", {})
        raw_path = str(settings.get("outlook_classic_path", "")).strip() if isinstance(settings, dict) else ""
        path = Path(raw_path) if raw_path else Path()
        if not raw_path or not path.exists():
            raise RuntimeError(f"Outlook Classic bulunamadi: {path}")
        subprocess.Popen([str(path)], cwd=str(path.parent))
        log(f"Outlook Classic baslatildi: {path}")

    def _forticlient_vpn_executable(self) -> Path | None:
        """Return Fortinet's newer documented Windows VPN CLI executable."""
        candidates = (
            Path("C:/Program Files/Fortinet/FortiClient/FortiVPN.exe"),
            Path("C:/Program Files (x86)/Fortinet/FortiClient/FortiVPN.exe"),
        )
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        return executable

    def _forticlient_console_executable(self) -> Path:
        """Return the FortiClient desktop executable used by 7.0.x clients.

        FortiClient 7.0.x does not ship ``FortiVPN.exe``.  Its supported
        profile behaviour is to establish the configured autoconnect tunnel
        when the FortiClient desktop process launches.
        """
        candidates = (
            Path("C:/Program Files/Fortinet/FortiClient/FortiClient.exe"),
            Path("C:/Program Files (x86)/Fortinet/FortiClient/FortiClient.exe"),
        )
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            raise RuntimeError(
                "FortiClient.exe bulunamadi. Once FortiClient VPN Kurulum adimini tamamlayin."
            )
        return executable

    def _start_forticlient_profile_autoconnect(self, log: Logger) -> None:
        """Start the 7.0.x console so its imported autoconnect profile runs.

        The profile keeps ``minimize_window_on_connect`` enabled.  FortiClient
        can therefore hand the connection attempt to its background tray/IPsec
        processes without requiring a person to click Connect.
        """
        console = self._forticlient_console_executable()
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        subprocess.Popen(
            [str(console)],
            cwd=str(console.parent),
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(
            "FortiClient 7.0 uyumluluk akisi baslatildi; profil otomatik olarak "
            f"{FORTICLIENT_VPN_CONNECTION_NAME} tuneline baglanmayi deniyor."
        )

    @staticmethod
    def _forticlient_tunnel_status(output: str) -> str:
        """Extract the requested tunnel state from FortiVPN's documented CLI output."""
        pattern = re.compile(
            rf"^\s*{re.escape(FORTICLIENT_VPN_CONNECTION_NAME)}\s*::\s*(?P<status>[^\r\n]+)",
            re.IGNORECASE | re.MULTILINE,
        )
        match = pattern.search(output or "")
        return match.group("status").strip().casefold() if match else ""

    def connect_forticlient(self, log: Logger) -> list[UiMessage]:
        """Connect the imported FortiClient tunnel with the supported Windows CLI.

        No password is passed on a process command line. FortiVPN uses the
        connection's own saved credentials, certificate, or configured
        authentication policy. This keeps credentials out of diagnostics and
        process listings while still asking FortiClient to connect directly.
        """
        executable = self._forticlient_vpn_executable()
        if executable is None:
            # FortiClient 7.0.14, which is the installer payload deployed by
            # this workflow, has no FortiVPN.exe command-line binary.  The
            # profile specifies its autoconnect tunnel, so importing it and
            # launching FortiClient starts the connection in the background.
            self.import_forticlient_vpn_profile(log)
            self.configure_forticlient_save_login(log)
            self._start_forticlient_profile_autoconnect(log)
            return [
                (
                    "Basarili",
                    f"FortiClient arka planda baslatildi; {FORTICLIENT_VPN_CONNECTION_NAME} VPN baglantisi deneniyor.",
                    "info",
                )
            ]
        self.configure_forticlient_save_login(log)
        status_command = [
            str(executable),
            "--cli",
            "--status",
            "--tunnel",
            FORTICLIENT_VPN_CONNECTION_NAME,
        ]
        initial_status = self._forticlient_tunnel_status(
            self._run_quiet(status_command, timeout_seconds=15).stdout
        )
        if initial_status == "connected":
            log(f"FortiClient VPN zaten bagli: {FORTICLIENT_VPN_CONNECTION_NAME}")
            return [("Bilgi", f"{FORTICLIENT_VPN_CONNECTION_NAME} VPN zaten bagli.", "info")]

        connect_result = self._run_quiet(
            [
                str(executable),
                "--cli",
                "--connect",
                "--tunnel",
                FORTICLIENT_VPN_CONNECTION_NAME,
            ],
            timeout_seconds=45,
        )
        log(f"FortiClient VPN baglanti komutu gonderildi: {FORTICLIENT_VPN_CONNECTION_NAME}")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status_result = self._run_quiet(status_command, timeout_seconds=15)
            if self._forticlient_tunnel_status(status_result.stdout) == "connected":
                log(f"FortiClient VPN baglantisi dogrulandi: {FORTICLIENT_VPN_CONNECTION_NAME}")
                return [("Basarili", f"{FORTICLIENT_VPN_CONNECTION_NAME} VPN baglantisi kuruldu.", "info")]
            time.sleep(2)

        if connect_result.returncode != 0:
            raise RuntimeError(
                "FortiClient VPN baglanti komutu basarisiz oldu. Kaydedilmis VPN kimlik bilgilerini ve "
                "FortiGate erisimini kontrol edin."
            )
        raise RuntimeError(
            "FortiClient VPN bagli olarak dogrulanamadi. Kaydedilmis VPN kimlik bilgilerini ve "
            "FortiGate erisimini kontrol edin."
        )

    def ensure_wifi_ssid(self, required_ssid: str, log: Logger) -> None:
        required_ssid = required_ssid.strip()
        if not required_ssid:
            log("Ikinci faz icin zorunlu Wi-Fi tanimli degil.")
            return
        if self.get_connected_wifi_ssid().casefold() == required_ssid.casefold():
            log(f"Wi-Fi baglantisi dogrulandi: {required_ssid}")
            return
        log(f"Kayitli Wi-Fi profiline otomatik baglaniliyor: {required_ssid}")
        self._connect_existing_wifi_profile(required_ssid, log)

    def execute_post_login_tasks(self, state: dict[str, object], log: Logger) -> list[UiMessage]:
        validate_state(state)
        messages: list[UiMessage] = []
        password = self.unprotect_secret(str(state.get("credential_password_protected", "")))
        username = str(state.get("credential_username", "")).strip()
        progress_writer = self.write_user_phase_progress

        self._run_state_task(
            state,
            "wifi_ready",
            lambda: self.ensure_wifi_ssid(str(state.get("required_wifi_ssid", "")), log),
            "Kurumsal ag baglantisi dogrulandi.",
            log,
            messages,
            progress_writer,
        )
        file_server = state.get("file_server", {})
        if isinstance(file_server, dict):
            self._run_state_task(
                state,
                "main_file_server",
                lambda: self.connect_main_file_server(
                    str(file_server.get("host", "")),
                    str(file_server.get("share", "")),
                    username,
                    password,
                    str(file_server.get("shortcut_name", "File Server")),
                    log,
                ),
                "File Server baglantisi ve kisayolu tamamlandi.",
                log,
                messages,
                progress_writer,
            )
        printer = state.get("printer", {})
        if isinstance(printer, dict):
            self._run_state_task(
                state,
                "network_printer",
                lambda: self.connect_network_printer(
                    str(printer.get("host", "")),
                    str(printer.get("share", "")),
                    username,
                    password,
                    log,
                ),
                "Ag yazicisi baglantisi tamamlandi.",
                log,
                messages,
                progress_writer,
            )
        desktop = state.get("desktop_automation", {})
        if isinstance(desktop, dict):
            wallpaper_path = Path(str(desktop.get("wallpaper_target_path", "")))
            if str(state.get("target_user_type", "")).strip() == "Lokal":
                self._run_state_task(
                    state,
                    "desktop_wallpaper",
                    lambda: self.apply_wallpaper_for_current_user(
                        wallpaper_path,
                        bool(desktop.get("wallpaper_lock_change", False)),
                        log,
                    ),
                    "Kurumsal masaustu arka plani uygulandi.",
                    log,
                    messages,
                    progress_writer,
                )
            else:
                tasks = state.get("tasks", {})
                wallpaper_task = tasks.get("desktop_wallpaper", {}) if isinstance(tasks, dict) else {}
                if isinstance(wallpaper_task, dict) and bool(wallpaper_task.get("enabled")):
                    mark_task(
                        state,
                        "desktop_wallpaper",
                        TASK_SKIPPED,
                        "Domain kullanicisi icin sabit yerel duvar kagidi uygulanmaz.",
                    )
                    log("Domain kullanicisi icin sabit duvar kagidi gorevi guvenle atlandi.")
        self._run_state_task(
            state,
            "desktop_signature",
            lambda: self._deploy_signature_from_state(state, log),
            "Kurumsal imza dosyalari masaustune kopyalandi.",
            log,
            messages,
            progress_writer,
        )
        self._run_state_task(
            state,
            "classic_outlook",
            lambda: self._launch_outlook_from_state(state, log),
            "Outlook Classic baslatildi.",
            log,
            messages,
            progress_writer,
        )
        self._run_state_task(
            state,
            "windows_update",
            lambda: self.open_windows_update_page(log),
            "Windows Update sayfasi acildi.",
            log,
            messages,
            progress_writer,
        )
        return messages

    def _write_post_login_result(
        self,
        state: dict[str, object],
        messages: list[UiMessage],
        error: str = "",
    ) -> None:
        status = workflow_status(state)
        payload: dict[str, object] = {
            "run_id": state.get("run_id", ""),
            "status": status,
            "completed_at": utc_now(),
            "messages": [list(message) for message in messages],
            "error": error,
        }
        atomic_write_json(self.post_login_result_path(), payload)

    def _update_report_from_state(self, state: dict[str, object], log: Logger) -> None:
        raw_path = str(state.get("report_path", "")).strip()
        if not raw_path:
            return
        report_path = Path(raw_path)
        if not report_path.exists():
            log(f"Post-login raporu henuz bulunamadi: {report_path}")
            return
        try:
            report = read_json(report_path)
            report["post_login"] = {
                "status": workflow_status(state),
                "user_phase": phase_status(state, "user"),
                "system_phase": phase_status(state, "system"),
                "tasks": state.get("tasks", {}),
                "updated_at": utc_now(),
            }
            if workflow_status(state) in {"completed", "partial"}:
                report["status"] = workflow_status(state)
            atomic_write_json(report_path, report)
        except Exception as exc:
            log(f"Post-login raporu guncellenemedi: {exc}")

    def _set_phase_state(self, state: dict[str, object], phase: str) -> None:
        phases = state.setdefault("phases", {})
        if not isinstance(phases, dict):
            raise RuntimeError("Is akisi faz bilgisi gecersiz.")
        phase_data = phases.setdefault(phase, {})
        if not isinstance(phase_data, dict):
            raise RuntimeError(f"Is akisi fazi gecersiz: {phase}")
        phase_data["status"] = phase_status(state, phase)
        phase_data["updated_at"] = utc_now()

    def get_current_identity(self, log: Logger) -> tuple[str, str]:
        if sys.platform != "win32":
            username = os.environ.get("USERNAME", "").strip()
            domain = os.environ.get("USERDOMAIN", "").strip()
            return (f"{domain}\\{username}" if domain else username, "")
        completed = self.run_powershell(
            "$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent(); "
            '"{0}|{1}" -f $identity.Name, $identity.User.Value',
            log,
            check=False,
        )
        rows = [line.strip() for line in completed.stdout.splitlines() if "|" in line]
        if not rows:
            return ("", "")
        name, sid = rows[-1].split("|", 1)
        return name.strip(), sid.strip()

    def handle_deferred_startup(
        self,
        log: Logger,
        run_id: str = "",
    ) -> list[UiMessage]:
        if not run_id:
            result_path = self.post_login_result_path()
            if not result_path.exists():
                log("Tamamlanmis ikinci faz sonucu bulunmuyor.")
                return []
            result = read_json(result_path)
            result_path.unlink(missing_ok=True)
            return [
                tuple(item)
                for item in result.get("messages", [])
                if isinstance(item, list) and len(item) == 3
            ]

        state = self.load_user_phase_state(run_id, log)
        current_name, current_sid = self.get_current_identity(log)
        target_principal = str(state.get("target_principal", "")).strip()
        target_sid = str(state.get("target_sid", "")).strip()
        identity_matches = bool(
            (target_sid and current_sid and target_sid.casefold() == current_sid.casefold())
            or (
                target_principal
                and current_name
                and target_principal.casefold() == current_name.casefold()
            )
        )
        if not identity_matches:
            raise RuntimeError(
                "Kullanici fazi yanlis Windows kimliginde baslatildi. "
                f"Hedef: {target_principal}, mevcut: {current_name or 'bilinmiyor'}"
            )
        if not enabled_phase_tasks(state, "user"):
            return []

        log(f"Kullanici fazi baslatildi. Run ID: {state.get('run_id', '')}")
        messages = self.execute_post_login_tasks(state, log)
        self.finalize_retryable_phase_tasks(
            state,
            "user",
            log,
            "Kullanici fazi bu oturumda tamamlanamadi; diger zorunlu adimlar engellenmedi.",
        )
        self._set_phase_state(state, "user")

        user_status = phase_status(state, "user")
        if user_status == "pending":
            messages.append(
                (
                    "Uyari",
                    "Bazi kullanici gorevleri tamamlanamadi. Sonraki oturumda yalnizca eksik adimlar yeniden denenecek.",
                    "warning",
                )
            )
        elif user_status == "partial":
            messages.append(
                (
                    "Hata",
                    "Kullanici fazi kalici hatayla kismi tamamlandi. X temizleme zinciri guvenli dogrulamayla devam edecek.",
                    "error",
                )
            )
        state["user_messages"] = messages
        self.write_user_phase_progress(state, log)
        log(f"Kullanici fazi durumu: {user_status}")
        return messages

    def get_interactive_username(self, log: Logger) -> str:
        completed = self.run_powershell(
            "(Get-CimInstance Win32_ComputerSystem).UserName",
            log,
            check=False,
        )
        rows = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if rows:
            return rows[-1]

        # Some hardened images deny CIM reads to a scheduled SYSTEM task even
        # though the account has an active desktop.  ``query user`` is the
        # same source used for the safe X handoff and gives us a reliable
        # fallback without weakening the target-account checks.
        fallback = self.run_powershell("& query.exe user", log, check=False)
        for raw_line in fallback.stdout.splitlines():
            line = raw_line.lstrip().lstrip(">").strip()
            columns = line.split()
            if columns and columns[0].casefold() != "username" and any(
                value.casefold() == "active" for value in columns
            ):
                log(f"Etkin Windows oturumu query user ile algilandi: {columns[0]}")
                return columns[0]
        return ""

    def logoff_legacy_session_for_target_handoff(
        self,
        state: dict[str, object],
        log: Logger,
    ) -> None:
        """Log off a surprise X auto-sign-in without deleting its profile.

        SYSTEM finalization must not delete X while that profile is active.
        If an image or policy signs X in again despite the handoff setting,
        ending only that session returns Windows to the account picker.  The
        durable task then waits for the intended new user to sign in before
        doing any privileged or destructive work.
        """
        cleanup_user = str(
            state.get("legacy_cleanup_user", self.config.legacy_cleanup_user)
        ).strip()
        target_username = str(state.get("target_username", "")).strip()
        local_admin = str(
            state.get("local_admin_username", self.config.tools.local_admin_username)
        ).strip()
        protected = {target_username.casefold(), local_admin.casefold(), "administrator"}
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", cleanup_user):
            raise RuntimeError("Oturumu kapatilacak eski kullanici adi guvenli degil.")
        if cleanup_user.casefold() in protected:
            raise RuntimeError("Hedef veya lokaladm kullanicisinin oturumu kapatilamaz.")
        script = f"""
$ErrorActionPreference = 'Stop'
$userName = '{self.ps_escape(cleanup_user)}'
$sessionLines = @(& query.exe user $userName 2>&1)
$sessionIds = @()
foreach ($line in $sessionLines) {{
    if ($line -match ('^\\s*>?' + [regex]::Escape($userName) + '\\s+(?<tail>.*)$')) {{
        # A disconnected session can have an empty SESSIONNAME column. The
        # first standalone number after the username is the session ID.
        $sessionId = @($Matches['tail'] -split '\\s+' | Where-Object {{ $_ -match '^\\d+$' }} | Select-Object -First 1)
        if ($sessionId) {{ $sessionIds += $sessionId }}
    }}
}}
$uniqueSessionIds = @($sessionIds | Select-Object -Unique)
if ($uniqueSessionIds.Count -lt 1) {{
    throw 'X etkin oturumu query user ile bulunamadi.'
}}
foreach ($sessionId in $uniqueSessionIds) {{
    & logoff.exe $sessionId
    if ($LASTEXITCODE -ne 0) {{ throw "X oturumu kapatilamadi: $sessionId" }}
}}
Write-Output "Legacy X sessions logged off: $($uniqueSessionIds.Count)"
"""
        self.run_powershell(script, log)
        log("X oturumu kapatildi; Windows kullanici secim ekraninda hedef kullaniciyi bekliyor.")

    def apply_wallpaper_lock_policy(self, state: dict[str, object], log: Logger) -> None:
        """Lock the wallpaper only for the intended local standard account.

        ``Desktop Wallpaper`` and ``Prevent changing desktop background`` are
        User Configuration policies. Applying them under HKLM would also lock
        domain accounts on this computer. This SYSTEM task writes the
        documented policy values only into the target local SID hive.

        The user phase normally leaves that hive loaded, but a fast logoff or
        a delayed retry must not turn a valid policy into a false failure.
        When necessary, SYSTEM temporarily mounts the account's NTUSER.DAT,
        applies the two policy keys, verifies them, and unloads it again.
        """
        if str(state.get("target_user_type", "")).strip() != "Lokal":
            raise RuntimeError(
                "Sabit duvar kagidi politikasi yalnizca yerel kullanici icin calistirilabilir."
            )
        options = state.get("options", {})
        if isinstance(options, dict) and bool(options.get("administrator")):
            raise RuntimeError(
                "Sabit duvar kagidi politikasi yonetici kullanici icin calistirilamaz."
            )
        settings = state.get("desktop_automation", {})
        raw_path = str(settings.get("wallpaper_target_path", "")).strip() if isinstance(settings, dict) else ""
        path = Path(raw_path)
        if not path.is_file():
            raise RuntimeError(f"Duvar kagidi gorseli bulunamadi: {path}")
        target_sid = str(state.get("target_sid", "")).strip()
        if not re.fullmatch(r"S-1-5-21(?:-\d+){4}", target_sid):
            raise RuntimeError("Yerel standart kullanici SID'i dogrulanamadi.")
        wallpaper = self.ps_escape(str(path.resolve()))
        target_sid = self.ps_escape(target_sid)
        script = f"""
$ErrorActionPreference = 'Stop'
$wallpaper = '{wallpaper}'
$targetSid = '{target_sid}'
$targetHive = "Registry::HKEY_USERS\\$targetSid"
$temporaryHiveName = 'ACIK-Wallpaper-' + ($targetSid -replace '[^A-Za-z0-9]', '')
$temporaryHiveKey = "HKU\\$temporaryHiveName"
$hiveLoadedHere = $false

if (-not (Test-Path -LiteralPath $targetHive)) {{
    # The SID is already validated by Python. Resolve its profile through
    # ProfileList rather than constructing C:\\Users from the account name.
    $profileKey = "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\$targetSid"
    $profile = Get-ItemProperty -LiteralPath $profileKey -ErrorAction Stop
    $profilePath = [Environment]::ExpandEnvironmentVariables([string]$profile.ProfileImagePath)
    $ntUserDat = Join-Path -Path $profilePath -ChildPath 'NTUSER.DAT'
    if (-not (Test-Path -LiteralPath $ntUserDat -PathType Leaf)) {{
        throw "Hedef yerel kullanici profili bulunamadi: $ntUserDat"
    }}
    & reg.exe load $temporaryHiveKey $ntUserDat
    if ($LASTEXITCODE -ne 0) {{
        throw "Hedef yerel kullanici kayit profili yuklenemedi. Kod: $LASTEXITCODE"
    }}
    $targetHive = "Registry::HKEY_USERS\\$temporaryHiveName"
    $hiveLoadedHere = $true
}}

try {{
    $systemPolicyPath = "$targetHive\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System"
    $activeDesktopPolicyPath = "$targetHive\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\ActiveDesktop"
    New-Item -Path $systemPolicyPath -Force | Out-Null
    New-Item -Path $activeDesktopPolicyPath -Force | Out-Null

# Documented User Configuration policy mappings:
# Desktop Wallpaper: Policies\\System\\Wallpaper (+ WallpaperStyle)
# Prevent changing desktop background: Policies\\ActiveDesktop\\NoChangingWallPaper
Set-ItemProperty -Path $systemPolicyPath -Name 'Wallpaper' -Value $wallpaper -Force
Set-ItemProperty -Path $systemPolicyPath -Name 'WallpaperStyle' -Value '10' -Force
Set-ItemProperty -Path $activeDesktopPolicyPath -Name 'NoChangingWallPaper' -Type DWord -Value 1 -Force

# A standard user normally owns HKCU. Protect only the two AÇIK policy
# keys, never the whole Policies branch: broader ACL replacement can break
# unrelated Windows or application policies in that profile.
$systemIdentity = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$administratorsIdentity = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$targetIdentity = [System.Security.Principal.SecurityIdentifier]::new($targetSid)
$inherit = [System.Security.AccessControl.InheritanceFlags]::None
$propagation = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$policyAcl = [System.Security.AccessControl.RegistrySecurity]::new()
$policyAcl.SetOwner($systemIdentity)
$policyAcl.SetAccessRuleProtection($true, $false)
$policyAcl.AddAccessRule([System.Security.AccessControl.RegistryAccessRule]::new($systemIdentity, [System.Security.AccessControl.RegistryRights]::FullControl, $inherit, $propagation, $allow))
$policyAcl.AddAccessRule([System.Security.AccessControl.RegistryAccessRule]::new($administratorsIdentity, [System.Security.AccessControl.RegistryRights]::FullControl, $inherit, $propagation, $allow))
$policyAcl.AddAccessRule([System.Security.AccessControl.RegistryAccessRule]::new($targetIdentity, [System.Security.AccessControl.RegistryRights]::ReadKey, $inherit, $propagation, $allow))
Set-Acl -LiteralPath $systemPolicyPath -AclObject $policyAcl
Set-Acl -LiteralPath $activeDesktopPolicyPath -AclObject $policyAcl

$actualWallpaper = (Get-ItemProperty -LiteralPath $systemPolicyPath -Name 'Wallpaper' -ErrorAction Stop).Wallpaper
$actualStyle = (Get-ItemProperty -LiteralPath $systemPolicyPath -Name 'WallpaperStyle' -ErrorAction Stop).WallpaperStyle
$actualLock = (Get-ItemProperty -LiteralPath $activeDesktopPolicyPath -Name 'NoChangingWallPaper' -ErrorAction Stop).NoChangingWallPaper
$actualSystemOwner = (Get-Acl -LiteralPath $systemPolicyPath).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
$actualActiveDesktopOwner = (Get-Acl -LiteralPath $activeDesktopPolicyPath).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
if ($actualWallpaper -ne $wallpaper -or $actualStyle -ne '10' -or [int]$actualLock -ne 1 -or $actualSystemOwner -ne 'S-1-5-18' -or $actualActiveDesktopOwner -ne 'S-1-5-18') {{
    throw 'Duvar kagidi kilitleme ilkesi dogrulanamadi.'
}}
}} finally {{
    if ($hiveLoadedHere) {{
        # Release registry-provider handles before unmounting an offline hive.
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
        & reg.exe unload $temporaryHiveKey
        if ($LASTEXITCODE -ne 0) {{
            throw "Hedef yerel kullanici kayit profili kaldirilamadi. Kod: $LASTEXITCODE"
        }}
    }}
}}
"""
        self.run_powershell(script, log)
        log("Duvar kagidi SYSTEM ilkesi ile standart kullaniciya karsı kilitlendi.")

    def apply_lock_screen_machine_policy(self, source: Path, log: Logger) -> None:
        """Apply the lock screen before the X-cleanup reboot, when supported.

        The locked pre-logon image is a device setting.  It must not wait for
        the new user to sign in, and it must never claim that a registry write
        works on a Windows Pro device where the supported CSP is unavailable.
        """
        if not source.is_file():
            raise RuntimeError(f"Kilit ekrani gorseli bulunamadi: {source}")
        staged_path = self.stage_lock_screen_asset(source, log)
        lock_screen_path = str(staged_path.resolve())
        lock_screen_uri = staged_path.resolve().as_uri()
        script = f"""
$ErrorActionPreference = 'Stop'
$policyPath = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization'
$lockScreenPath = '{self.ps_escape(lock_screen_path)}'
$lockScreenUri = '{self.ps_escape(lock_screen_uri)}'
$edition = [string](Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' -ErrorAction Stop).ProductName
if (-not (Test-Path -LiteralPath $lockScreenPath -PathType Leaf)) {{
    throw "Kilit ekrani dosyasi SYSTEM tarafindan okunamadi: $lockScreenPath"
}}

function Set-ChangeLockAndVerify {{
    New-Item -Path $policyPath -Force | Out-Null
    Set-ItemProperty -Path $policyPath -Name 'NoChangingLockScreen' -Type DWord -Value 1 -Force
    $effective = Get-ItemProperty -Path $policyPath -ErrorAction Stop
    if ([int]$effective.NoChangingLockScreen -ne 1) {{
        throw 'Kilit ekrani degistirme kilidi dogrulanamadi.'
    }}
}}

# The official Computer Configuration policy is the durable pre-logon path
# for Enterprise/Education.  It uses a local ProgramData file rather than the
# USB drive, which may not exist when LockApp starts after reboot.
$isEnterpriseOrEducation = ($edition -match 'Enterprise') -or (($edition -match 'Education') -and ($edition -notmatch 'Pro'))
if ($isEnterpriseOrEducation) {{
    New-Item -Path $policyPath -Force | Out-Null
    Set-ItemProperty -Path $policyPath -Name 'LockScreenImage' -Value $lockScreenPath -Force
    Set-ChangeLockAndVerify
    $effective = Get-ItemProperty -Path $policyPath -ErrorAction Stop
    if ([string]$effective.LockScreenImage -ne $lockScreenPath) {{
        throw 'Kilit ekrani Group Policy hedef yolu dogrulanamadi.'
    }}
    Write-Output 'LOCK_SCREEN_METHOD=EnterpriseEducationGroupPolicy'
    exit 0
}}

# Windows Pro does not support the ForceDefaultLockScreen Group Policy.  The
# only supported route is the device Personalization CSP on a device already
# managed for that CSP (for example MDM, or the documented SharedPC education
# configuration).  Do not change the device into an education environment
# merely to make this setting appear to work.
$cspNamespace = 'root\\cimv2\\mdm\\dmmap'
$cspClass = 'MDM_Personalization'
$cspFilter = "ParentID='./Vendor/MSFT/' and InstanceID='Personalization'"
try {{
    $personalization = Get-CimInstance -Namespace $cspNamespace -ClassName $cspClass -Filter $cspFilter -ErrorAction SilentlyContinue
    if ($null -eq $personalization) {{
        $personalization = New-CimInstance -Namespace $cspNamespace -ClassName $cspClass -Property @{{
            ParentID = './Vendor/MSFT/'
            InstanceID = 'Personalization'
            LockScreenImageUrl = $lockScreenUri
        }} -ErrorAction Stop
    }} else {{
        $personalization | Set-CimInstance -Property @{{ LockScreenImageUrl = $lockScreenUri }} -ErrorAction Stop | Out-Null
    }}

    $status = 0
    $deadline = (Get-Date).AddSeconds(30)
    do {{
        Start-Sleep -Seconds 1
        $personalization = Get-CimInstance -Namespace $cspNamespace -ClassName $cspClass -Filter $cspFilter -ErrorAction Stop
        $status = [int]$personalization.LockScreenImageStatus
        if ([string]$personalization.LockScreenImageUrl -ne $lockScreenUri) {{
            throw 'Personalization CSP farkli bir kilit ekrani yolu bildirdi.'
        }}
        if ($status -eq 1) {{ break }}
        if ($status -in 3, 4, 5, 6) {{
            throw "Personalization CSP kilit ekrani kopyasini kabul etmedi (durum: $status)."
        }}
    }} while ((Get-Date) -lt $deadline)
    if ($status -ne 1) {{
        throw "Personalization CSP kilit ekrani zaman asimina ugradi (durum: $status)."
    }}
    Set-ChangeLockAndVerify
    Write-Output 'LOCK_SCREEN_METHOD=ManagedPersonalizationCSP'
}} catch {{
    if ($edition -match 'Pro') {{
        throw "Windows Pro bu cihazda desteklenen Personalization CSP ile yonetilmiyor. Ozel kilit ekrani, MDM/Intune veya onceden ayarlanmis SharedPC SetEduPolicies gerektirir; kurulum cihaz rolunu degistirmedi. Ayrinti: $($_.Exception.Message)"
    }}
    throw "Kilit ekrani Personalization CSP ile uygulanamadi: $($_.Exception.Message)"
}}
"""
        self.run_powershell(script, log)
        log(f"Kilit ekrani gorseli ve desteklenen degistirme kilidi dogrulandi: {lock_screen_path}")

    def apply_lock_screen_policy(self, state: dict[str, object], log: Logger) -> None:
        """Resolve and apply the packaged lock-screen asset during SYSTEM finalize.

        The lock-screen image is a static packaged/config asset, not something
        bundled per onboarding request, so it is resolved fresh here (the same
        way the preflight check does) instead of being threaded through
        ``desktop_automation`` state. By the time SYSTEM finalize runs, the
        app already runs from its ``%ProgramData%\\AcikOnboarding\\app`` copy,
        so this does not depend on the original USB drive letter staying put.
        """
        del state  # kept for the _run_state_task(state, name, callback, ...) call shape
        source = self.resolve_lock_screen_source()
        self.apply_lock_screen_machine_policy(source, log)

    def install_eset_from_state(self, state: dict[str, object], log: Logger) -> None:
        installer = Path(str(state.get("eset_installer_path", "")))
        if not installer.exists():
            raise RuntimeError(f"ESET yukleyicisi bulunamadi: {installer}")
        self.verify_payload_integrity(installer)
        original_path = self.config.tools.eset_installer_path
        self.config.tools.eset_installer_path = str(installer)
        try:
            self.run_eset_installer(log)
        finally:
            self.config.tools.eset_installer_path = original_path
        for _ in range(18):
            if self.is_program_installed("eset"):
                log("ESET kurulumu servis/registry uzerinden dogrulandi.")
                return
            time.sleep(5)
        raise RuntimeError("ESET yukleyicisi kapandi ancak kurulum dogrulanamadi.")

    def delete_legacy_user_verified(self, state: dict[str, object], log: Logger) -> None:
        cleanup_user = str(state.get("legacy_cleanup_user", "")).strip()
        target_user = str(state.get("target_username", "")).strip()
        local_admin = str(state.get("local_admin_username", "lokaladm")).strip()
        current_user = self.get_interactive_username(log).split("\\")[-1]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", cleanup_user):
            raise RuntimeError("Silinecek eski kullanici adi guvenli degil.")
        protected = {target_user.casefold(), local_admin.casefold(), current_user.casefold(), "administrator"}
        if cleanup_user.casefold() in protected:
            raise RuntimeError("Korunan veya aktif bir kullanici silinmeye calisildi.")

        script = f"""
$ErrorActionPreference = 'Stop'
# Some Windows images cannot load Microsoft.PowerShell.LocalAccounts from a
# SYSTEM scheduled task.  The older deployed installer still removed X there
# through ``net user /delete``; retain that fallback instead of abandoning the
# entire cleanup before the account/profile are touched.
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction SilentlyContinue
$userName = '{self.ps_escape(cleanup_user)}'
$expectedRoot = [IO.Path]::GetFullPath('C:\\Users') + [IO.Path]::DirectorySeparatorChar
$hasLocalAccountsCmdlet = [bool](Get-Command Get-LocalUser -ErrorAction SilentlyContinue)
$user = if ($hasLocalAccountsCmdlet) {{ Get-LocalUser -Name $userName -ErrorAction SilentlyContinue }} else {{ $null }}
$sid = if ($user) {{ $user.SID.Value }} else {{ '' }}
$profile = Get-CimInstance Win32_UserProfile -ErrorAction Stop |
    Where-Object {{ ($sid -and $_.SID -eq $sid) -or $_.LocalPath -ieq ('C:\\Users\\' + $userName) }} |
    Select-Object -First 1
if ($profile -and $profile.Special) {{ throw 'Ozel bir Windows profili silinemez.' }}
$profilePath = if ($profile) {{ [IO.Path]::GetFullPath($profile.LocalPath) }} else {{ [IO.Path]::GetFullPath('C:\\Users\\' + $userName) }}
if (-not $profilePath.StartsWith($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {{
    throw "Profil yolu C:\\Users disinda: $profilePath"
}}
$sessionLines = @(& query.exe user $userName 2>$null | Select-Object -Skip 1)
foreach ($line in $sessionLines) {{
    if ($line -match ('^\\s*>?' + [regex]::Escape($userName) + '\\s+(?<tail>.*)$')) {{
        # A disconnected session can have an empty SESSIONNAME column. Pick
        # the first standalone number after the username: that is the ID in
        # both console/RDP and disconnected query-user layouts.
        $sessionId = @($Matches['tail'] -split '\\s+' | Where-Object {{ $_ -match '^\\d+$' }} | Select-Object -First 1)
        if ($sessionId) {{
            & logoff.exe $sessionId
            if ($LASTEXITCODE -ne 0) {{ throw "Oturum kapatilamadi: $sessionId" }}
        }}
    }}
}}
# Profile unloading is asynchronous after logoff.  Checking the stale CIM
# object immediately made X cleanup fail on the first and usually only target
# login, leaving the workflow permanently pending. Re-query for up to 30s.
for ($attempt = 1; $attempt -le 15; $attempt++) {{
    $profile = Get-CimInstance Win32_UserProfile -ErrorAction Stop |
        Where-Object {{ ($sid -and $_.SID -eq $sid) -or $_.LocalPath -ieq ('C:\\Users\\' + $userName) }} |
        Select-Object -First 1
    if (-not $profile -or -not $profile.Loaded) {{ break }}
    Start-Sleep -Seconds 2
}}
if ($profile -and $profile.Loaded) {{
    throw 'Eski kullanici profili halen yuklu; oturum kapanmasi sonrasi yeniden denenecek.'
}}

# Account removal comes before the profile provider and file fallback.  This
# is the sequence used by the known-good legacy package and avoids leaving an
# account behind if an old profile contains protected files.  Prefer the
# LocalAccounts cmdlet, but use net.exe when that module is unavailable or
# rejects the call in the SYSTEM task.
$accountRemoved = if ($hasLocalAccountsCmdlet) {{ -not $user }} else {{ $false }}
if ($user -and $hasLocalAccountsCmdlet) {{
        try {{
            Remove-LocalUser -Name $userName -ErrorAction Stop
            $accountRemoved = $true
        }} catch {{
            Write-Output ('Remove-LocalUser failed; net.exe fallback will be used: ' + $_.Exception.Message)
        }}
}}
if (-not $accountRemoved) {{
    & net.exe user $userName /delete 2>$null
    if ($LASTEXITCODE -ne 0) {{ throw "Eski kullanici hesabi silinemedi (net.exe kodu: $LASTEXITCODE)." }}
}}
if ($hasLocalAccountsCmdlet) {{
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {{
        throw 'Eski kullanici hesabi silinemedi.'
    }}
}} else {{
    & net.exe user $userName 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {{ throw 'Eski kullanici hesabi silinemedi.' }}
}}

# Let the Windows profile provider remove the ProfileList entry and ordinary
# profile content before using the guarded tree fallback below.  Its removal
# is safe now because the logoff/unload check above has already passed.
if ($profile) {{
    try {{
        Remove-CimInstance -InputObject $profile -ErrorAction Stop
        $profile = $null
    }} catch {{
        Write-Output ('Win32_UserProfile cleanup failed; guarded file fallback will continue: ' + $_.Exception.Message)
    }}
}}
$profileItem = Get-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue
if ($profileItem -and ($profileItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{
    throw 'Profil kok klasoru reparse point; guvenli silme reddedildi.'
}}

function Remove-SafeProfileTree([string] $Path) {{
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)) {{
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {{
            # Standard Windows profiles contain compatibility junctions such as
            # "Application Data". Remove the link itself; never recurse into it.
            if ($item.PSIsContainer) {{
                [IO.Directory]::Delete($item.FullName, $false)
            }} else {{
                Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
            }}
        }} elseif ($item.PSIsContainer) {{
            Remove-SafeProfileTree $item.FullName
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
        }} else {{
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
        }}
    }}
}}
if (Test-Path -LiteralPath $profilePath) {{
    & takeown.exe /f $profilePath /d y | Out-Null
    if ($LASTEXITCODE -ne 0) {{ throw 'Profil sahipligi alinamadi.' }}
    $adminSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $adminGroup = $adminSid.Translate([System.Security.Principal.NTAccount]).Value
    & icacls.exe $profilePath /grant "$adminGroup`:(OI)(CI)F" /c | Out-Null
    if ($LASTEXITCODE -ne 0) {{ throw 'Profil ACL duzeltmesi basarisiz.' }}
    Remove-SafeProfileTree $profilePath
    Remove-Item -LiteralPath $profilePath -Force -ErrorAction Stop
}}
if (Test-Path -LiteralPath $profilePath) {{
    throw 'Eski kullanici profili silinemedi.'
}}

# Remove a stale ProfileList entry only after account deletion and filesystem
# verification.  Some Windows builds retain the SID registry key even though
# C:\\Users\\x is already gone, which makes management tools keep showing a
# phantom X profile.  Match by the validated profile path; never remove an
# unrelated SID entry.
$profileListRoot = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList'
$staleProfileKeys = @(
    Get-ChildItem -LiteralPath $profileListRoot -ErrorAction Stop | Where-Object {{
        $registeredPath = [string](Get-ItemProperty -LiteralPath $_.PSPath -Name 'ProfileImagePath' -ErrorAction SilentlyContinue).ProfileImagePath
        $registeredPath -ieq $profilePath
    }}
)
foreach ($profileKey in $staleProfileKeys) {{
    Remove-Item -LiteralPath $profileKey.PSPath -Recurse -Force -ErrorAction Stop
}}
$remainingProfileKey = Get-ChildItem -LiteralPath $profileListRoot -ErrorAction Stop | Where-Object {{
    $registeredPath = [string](Get-ItemProperty -LiteralPath $_.PSPath -Name 'ProfileImagePath' -ErrorAction SilentlyContinue).ProfileImagePath
    $registeredPath -ieq $profilePath
}} | Select-Object -First 1
if ($remainingProfileKey) {{
    throw 'Silinen eski kullanici icin ProfileList kaydi kaldı.'
}}
Write-Output "Legacy user removal verified: $userName"
"""
        self.run_powershell(script, log)
        log(f"Eski kullanici ve profili dogrulanarak temizlendi: {cleanup_user}")

    def _schedule_system_restart_task(
        self,
        run_id: str,
        delay_seconds: int,
        log: Logger,
    ) -> str:
        """Schedule a verified SYSTEM restart, with an elevated fallback.

        ``shutdown.exe`` can return code 1 on some prepared images even from
        an elevated UI session.  Running the restart action as SYSTEM avoids
        that user-token dependency and keeps the reboot independent from the
        GUI countdown.
        """
        delay_seconds = max(5, min(300, int(delay_seconds)))
        safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:32] or uuid.uuid4().hex[:12]
        runtime_dir = self.system_runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        script_path = runtime_dir / f"initial_restart_{safe_run_id}.ps1"
        restart_log_path = runtime_dir / f"initial_restart_{safe_run_id}.log"
        task_name = f"AcikOnboardingInitialRestart-{safe_run_id[:12]}"
        script_path.write_text(
            f"""param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][string]$RestartLog
)
$ErrorActionPreference = 'Stop'
function Write-RestartLog([string]$Message) {{
    Add-Content -LiteralPath $RestartLog -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}}
try {{
    Write-RestartLog 'SYSTEM restart task started.'
    Start-Sleep -Seconds {delay_seconds}
    $shutdownExe = Join-Path $env:SystemRoot 'System32\\shutdown.exe'
    if (-not (Test-Path -LiteralPath $shutdownExe -PathType Leaf)) {{
        throw "shutdown.exe not found: $shutdownExe"
    }}
    # Clear an abandoned request from an earlier failed run before issuing the
    # verified handoff reboot. Exit code is irrelevant when no request exists.
    & $shutdownExe /a 2>$null
    & $shutdownExe /r /t 0 /f
    if ($LASTEXITCODE -ne 0) {{
        Write-RestartLog "shutdown.exe exit code: $LASTEXITCODE; trying Restart-Computer."
        Restart-Computer -Force -ErrorAction Stop
    }}
    Write-RestartLog 'shutdown.exe accepted the restart request.'
}} catch {{
    Write-RestartLog "shutdown.exe error: $($_.Exception.Message); trying Restart-Computer."
    try {{
        Restart-Computer -Force -ErrorAction Stop
    }} catch {{
        Write-RestartLog "Restart-Computer error: $($_.Exception.Message)"
        exit 1
    }}
}} finally {{
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}}
""",
            encoding="utf-8",
        )
        arguments = subprocess.list2cmdline(
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-TaskName",
                task_name,
                "-RestartLog",
                str(restart_log_path),
            ]
        )
        registration = f"""
$ErrorActionPreference = 'Stop'
$scriptPath = '{self.ps_escape(str(script_path))}'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {{ throw 'Ilk yeniden baslatma betigi olusturulamadi.' }}
$scheduleService = Get-Service -Name 'Schedule' -ErrorAction Stop
if ($scheduleService.Status -ne 'Running') {{ Start-Service -Name 'Schedule' -ErrorAction Stop }}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{self.ps_escape(arguments)}'
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(15))
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName '{self.ps_escape(task_name)}'
Write-Output "ACIK_SYSTEM_RESTART_TASK_STARTED: {self.ps_escape(task_name)}"
"""
        try:
            self.run_powershell(registration, log)
        except Exception as task_exc:
            # A disabled/corrupted scheduler must not silently leave the
            # completed workflow on screen. Restart-Computer uses the already
            # elevated installer token and produces a detailed failure if it
            # cannot ask Windows to reboot.
            log(f"SYSTEM yeniden baslatma gorevi planlanamadi: {task_exc}; yedek yeniden baslatma deneniyor.")
            fallback = f"""
$ErrorActionPreference = 'Stop'
Start-Sleep -Seconds {delay_seconds}
$shutdownExe = Join-Path $env:SystemRoot 'System32\\shutdown.exe'
if (Test-Path -LiteralPath $shutdownExe -PathType Leaf) {{ & $shutdownExe /a 2>$null }}
Restart-Computer -Force -ErrorAction Stop
"""
            self.run_powershell(fallback, log)
            log(f"Yedek Restart-Computer istegi gonderildi: {delay_seconds} saniye.")
            return f"Restart-Computer/{delay_seconds}s"
        log(f"SYSTEM yeniden baslatma gorevi baslatildi: {task_name}; {delay_seconds} saniye.")
        return task_name

    def schedule_initial_restart_after_setup(self, request: OnboardingRequest, log: Logger) -> str:
        """Guarantee the first handoff reboot without relying on a UI timer."""
        if not self.is_admin_session():
            raise RuntimeError("Otomatik yeniden baslatma icin yonetici yetkisi gerekli.")

        run_id = request.run_id or uuid.uuid4().hex
        request.run_id = run_id
        delay_seconds = max(30, min(300, int(self.config.windows.restart_delay_seconds)))
        task_name = self._schedule_system_restart_task(run_id, delay_seconds, log)
        try:
            state = self.load_workflow_state()
            if str(state.get("run_id", "")) == run_id and task_name.startswith("AcikOnboardingInitialRestart-"):
                state["initial_restart_task_name"] = task_name
                self.write_workflow_state(state, log)
        except Exception as exc:
            # The task is already registered and will still do the handoff;
            # this extra report field must not leave a half-configured device.
            log(f"Ilk yeniden baslatma gorevi durum dosyasina yazilamadi: {exc}")
        log(f"Yeni kullaniciya gecis icin yeniden baslatma planlandi: {task_name}")
        return task_name

    def restart_pending_workflow_for_handoff(self, log: Logger) -> str:
        """Restart a durable pending plan without discarding its post-login work.

        Operators sometimes have to restart manually before the first handoff.
        This recovery action keeps the protected state and scheduled tasks.
        When X cleanup was selected, it resumes the verified SYSTEM cleanup
        directly after the already-written report instead of merely asking
        Windows for another restart that could re-open X.
        """
        if not self.is_admin_session():
            raise RuntimeError("Bekleyen kurulumu yeniden baslatmak icin yonetici yetkisi gerekli.")
        state = self.load_workflow_state()
        run_id = str(state.get("run_id", "")).strip()
        target_username = str(state.get("target_username", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            raise RuntimeError("Bekleyen kurulumun run_id degeri gecersiz.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", target_username):
            raise RuntimeError("Bekleyen kurulumun hedef kullanicisi gecersiz.")

        options = state.get("options", {})
        delete_x_user = isinstance(options, dict) and bool(options.get("delete_x_user"))
        if delete_x_user:
            # LOCKED V5.7 X CLEANUP HANDOFF: the verified deployment clears
            # Winlogon and LSA AutoLogon before the target-account handoff.
            # Do not replace this sequence without an explicit V5.7-equivalent
            # deletion test on a real device.
            self.disable_automatic_signin_for_target_handoff(log)
            # This is a recovery handoff, not a replacement X remover.  If
            # the earlier immediate SYSTEM task never started, retain the
            # durable plan and let its normal SYSTEM finalizer delete and
            # verify X after the intended account has completed post-login.
            # Clearing this marker makes that finalizer request the final
            # reboot only after it has performed the verified deletion itself.
            tasks = state.get("tasks")
            if not isinstance(tasks, dict):
                raise RuntimeError("Bekleyen kurulumun gorev listesi gecersiz.")
            delete_task = tasks.get("delete_x_user")
            if not isinstance(delete_task, dict) or not bool(delete_task.get("enabled")):
                tasks["delete_x_user"] = {
                    "enabled": True,
                    "status": TASK_PENDING,
                    "attempts": 0,
                    "error": "",
                    "updated_at": utc_now(),
                }
            state["immediate_x_cleanup"] = False
            state["x_cleanup_recovery_handoff"] = True
            self.write_workflow_state(state, log)
            restart_task = self._schedule_system_restart_task(run_id, 5, log)
            log(
                "Bekleyen kurulum korunuyor; hedef kullaniciya gecis icin "
                f"SYSTEM yeniden baslatma planlandi ({restart_task}). "
                "Hedef oturum sonrasi SYSTEM X hesabini, profili ve ProfileList kaydini dogrulayarak silecek."
            )
            return target_username

        restart_task = self._schedule_system_restart_task(run_id, 5, log)
        log(
            f"Bekleyen kurulum korunarak yeniden baslatma planlandi ({restart_task}): 5 saniye. "
            f"Giris ekranindan {target_username} kullanicisini secin."
        )
        return target_username

    def disable_automatic_signin_for_target_handoff(self, log: Logger) -> None:
        """Show the Windows account picker after the X-to-target reboot.

        X can be configured for AutoAdminLogon on factory images. This runs
        only in the explicit X-cleanup workflow.  OEM images can store the
        automatic password in the LSA private-data store (the method used by
        Sysinternals AutoLogon), so changing only the ordinary Winlogon values
        is not sufficient.  Both stores are cleared without reading any secret
        and the absence of the LSA secret is verified before a reboot is
        allowed.  X stays enabled until the dedicated finalizer deletes the
        account and profile in one verified operation.
        """
        # LOCKED V5.7 X-cleanup pre-handoff: this block is intentionally kept
        # before the SYSTEM removal task because it is the only verified
        # sequence that removed X on the target hardware.
        if not self.is_admin_session():
            raise RuntimeError("Otomatik girisi kapatmak icin yonetici yetkisi gerekli.")
        script = """
$ErrorActionPreference = 'Stop'
$winlogonKeys = @(
    'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon',
    'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'
) | Where-Object { Test-Path -LiteralPath $_ }
if ($winlogonKeys.Count -lt 1) { throw 'Winlogon ayar anahtari bulunamadi.' }

# Do not read these values: DefaultPassword can contain a local credential.
$valuesToRemove = @(
    'DefaultUserName', 'DefaultDomainName', 'DefaultPassword',
    'AltDefaultUserName', 'AltDefaultDomainName', 'AltDefaultPassword',
    'ForceAutoLogon', 'AutoLogonCount'
)
foreach ($key in $winlogonKeys) {
    foreach ($name in $valuesToRemove) {
        Remove-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue
    }
    New-ItemProperty -Path $key -Name 'AutoAdminLogon' -PropertyType String -Value '0' -Force | Out-Null
    New-ItemProperty -Path $key -Name 'DisableAutomaticRestartSignOn' -PropertyType DWord -Value 1 -Force | Out-Null
    $effective = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
    if ([string]$effective.AutoAdminLogon -ne '0') {
        throw "Windows otomatik girisi devre disi birakilamadi: $key"
    }
    if ([int]$effective.DisableAutomaticRestartSignOn -ne 1) {
        throw "Otomatik yeniden baslatma girisi kapatilamadi: $key"
    }
}

# Do not write account-picker policy during the elevated X-session handoff.
# Some managed images deny Administrators access to this policy key even when
# the installer is elevated, which blocked X cleanup before its SYSTEM task
# could begin. The SYSTEM cleanup task applies and verifies the official
# local-user picker policy only after X is actually deleted, just before reboot.

# Sysinternals AutoLogon stores DefaultPassword as an LSA private-data secret.
# Registry reads cannot see it.  Use the LSA API only to delete and test for
# absence; the secret is never retrieved, logged, or copied.
$lsaSource = @'
using System;
using System.Runtime.InteropServices;

public static class AcikAutoLogonLsa
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LsaObjectAttributes
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LsaUnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [DllImport("advapi32.dll")]
    private static extern uint LsaOpenPolicy(
        IntPtr systemName,
        ref LsaObjectAttributes objectAttributes,
        uint desiredAccess,
        out IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaStorePrivateData(
        IntPtr policyHandle,
        ref LsaUnicodeString keyName,
        IntPtr privateData);

    [DllImport("advapi32.dll")]
    private static extern uint LsaRetrievePrivateData(
        IntPtr policyHandle,
        ref LsaUnicodeString keyName,
        out IntPtr privateData);

    [DllImport("advapi32.dll")]
    private static extern uint LsaFreeMemory(IntPtr buffer);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr policyHandle);

    private const uint PolicyGetPrivateInformation = 0x00000004;
    private const uint PolicyCreateSecret = 0x00000020;

    private static LsaUnicodeString MakeString(string value)
    {
        var buffer = Marshal.StringToHGlobalUni(value);
        return new LsaUnicodeString
        {
            Length = (ushort)(value.Length * 2),
            MaximumLength = (ushort)((value.Length + 1) * 2),
            Buffer = buffer,
        };
    }

    public static uint DeleteSecret(string name)
    {
        var attributes = new LsaObjectAttributes
        {
            Length = Marshal.SizeOf(typeof(LsaObjectAttributes)),
        };
        IntPtr policy;
        var status = LsaOpenPolicy(IntPtr.Zero, ref attributes, PolicyCreateSecret, out policy);
        if (status != 0) return status;
        var key = MakeString(name);
        try
        {
            return LsaStorePrivateData(policy, ref key, IntPtr.Zero);
        }
        finally
        {
            Marshal.FreeHGlobal(key.Buffer);
            LsaClose(policy);
        }
    }

    public static uint GetSecretStatus(string name)
    {
        var attributes = new LsaObjectAttributes
        {
            Length = Marshal.SizeOf(typeof(LsaObjectAttributes)),
        };
        IntPtr policy;
        var status = LsaOpenPolicy(IntPtr.Zero, ref attributes, PolicyGetPrivateInformation, out policy);
        if (status != 0) return status;
        var key = MakeString(name);
        IntPtr secret = IntPtr.Zero;
        try
        {
            return LsaRetrievePrivateData(policy, ref key, out secret);
        }
        finally
        {
            if (secret != IntPtr.Zero) LsaFreeMemory(secret);
            Marshal.FreeHGlobal(key.Buffer);
            LsaClose(policy);
        }
    }
}
'@
if ($null -eq ('AcikAutoLogonLsa' -as [type])) {
    Add-Type -TypeDefinition $lsaSource -ErrorAction Stop
}
$objectNotFound = [Convert]::ToUInt32('C0000034', 16)
$deleteStatus = [AcikAutoLogonLsa]::DeleteSecret('DefaultPassword')
if ($deleteStatus -ne 0 -and $deleteStatus -ne $objectNotFound) {
    throw "Guvenli AutoLogon saklamasi silinemedi (LSA durum: $deleteStatus)."
}
$verifyStatus = [AcikAutoLogonLsa]::GetSecretStatus('DefaultPassword')
if ($verifyStatus -ne $objectNotFound) {
    throw "Guvenli AutoLogon saklamasi kaldigi icin hedef kullaniciya gecis durduruldu (LSA durum: $verifyStatus)."
}

Write-Output 'Winlogon and secure AutoLogon state cleared and verified for target-account handoff.'
"""
        self.run_powershell(script, log)
        log(
            "X hesabi devre disi birakilmadi; standart ve guvenli AutoLogon "
            "saklamalari temizlendi. SYSTEM temizligi X silindikten sonra yerel "
            "kullanici secim ilkesini uygulayacak."
        )

    def defer_automatic_signin_cleanup_to_system_task(self, log: Logger) -> None:
        """Record that the verified SYSTEM X cleanup owns the handoff."""
        if not self.is_admin_session():
            raise RuntimeError("SYSTEM X temizligini planlamak icin yonetici yetkisi gerekli.")
        log(
            "X hesabi rapor tamamlandiktan sonra kanitlanmis SYSTEM silme gorevi ile "
            "oturumu kapatilarak, hesabi ve C:\\Users profili kaldirilarak temizlenecek."
        )

    def system_automatic_signin_cleanup_script(self) -> str:
        """Return the AutoLogon cleanup that must execute in the SYSTEM task."""
        return r"""
# Never read DefaultPassword.  SYSTEM removes ordinary Winlogon markers,
# preserves the normal local account picker, and verifies the optional LSA
# secret is absent before it can restart after X deletion.
$winlogonKeys = @(
    'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Winlogon'
) | Where-Object { Test-Path -LiteralPath $_ }
if ($winlogonKeys.Count -lt 1) { throw 'Winlogon ayar anahtari bulunamadi.' }

$valuesToRemove = @(
    'DefaultUserName', 'DefaultDomainName', 'DefaultPassword',
    'AltDefaultUserName', 'AltDefaultDomainName', 'AltDefaultPassword',
    'ForceAutoLogon', 'AutoLogonCount'
)
foreach ($key in $winlogonKeys) {
    foreach ($name in $valuesToRemove) {
        Remove-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue
    }
    New-ItemProperty -Path $key -Name 'AutoAdminLogon' -PropertyType String -Value '0' -Force | Out-Null
    New-ItemProperty -Path $key -Name 'DisableAutomaticRestartSignOn' -PropertyType DWord -Value 1 -Force | Out-Null
    $effective = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
    if ([string]$effective.AutoAdminLogon -ne '0') { throw "Windows otomatik girisi devre disi birakilamadi: $key" }
    if ([int]$effective.DisableAutomaticRestartSignOn -ne 1) { throw "Otomatik yeniden baslatma girisi kapatilamadi: $key" }
}

$systemPolicy = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-Item -Path $systemPolicy -Force | Out-Null
Set-ItemProperty -Path $systemPolicy -Name 'DontDisplayLastUserName' -Type DWord -Value 0 -Force
if ([int](Get-ItemProperty -LiteralPath $systemPolicy -Name 'DontDisplayLastUserName' -ErrorAction Stop).DontDisplayLastUserName -ne 0) {
    throw 'Yerel kullanici secim ekrani ilkesi dogrulanamadi.'
}

$lsaSource = @'
using System;
using System.Runtime.InteropServices;
public static class AcikSystemAutoLogonLsa
{
    [StructLayout(LayoutKind.Sequential)] private struct LsaObjectAttributes { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
    [StructLayout(LayoutKind.Sequential)] private struct LsaUnicodeString { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [DllImport("advapi32.dll")] private static extern uint LsaOpenPolicy(IntPtr systemName, ref LsaObjectAttributes objectAttributes, uint desiredAccess, out IntPtr policyHandle);
    [DllImport("advapi32.dll")] private static extern uint LsaStorePrivateData(IntPtr policyHandle, ref LsaUnicodeString keyName, IntPtr privateData);
    [DllImport("advapi32.dll")] private static extern uint LsaRetrievePrivateData(IntPtr policyHandle, ref LsaUnicodeString keyName, out IntPtr privateData);
    [DllImport("advapi32.dll")] private static extern uint LsaFreeMemory(IntPtr buffer);
    [DllImport("advapi32.dll")] private static extern uint LsaClose(IntPtr policyHandle);
    private const uint PolicyGetPrivateInformation = 0x00000004;
    private const uint PolicyCreateSecret = 0x00000020;
    private static LsaUnicodeString MakeString(string value) { var buffer = Marshal.StringToHGlobalUni(value); return new LsaUnicodeString { Length = (ushort)(value.Length * 2), MaximumLength = (ushort)((value.Length + 1) * 2), Buffer = buffer }; }
    public static uint DeleteSecret(string name) { var attributes = new LsaObjectAttributes { Length = Marshal.SizeOf(typeof(LsaObjectAttributes)) }; IntPtr policy; var status = LsaOpenPolicy(IntPtr.Zero, ref attributes, PolicyCreateSecret, out policy); if (status != 0) return status; var key = MakeString(name); try { return LsaStorePrivateData(policy, ref key, IntPtr.Zero); } finally { Marshal.FreeHGlobal(key.Buffer); LsaClose(policy); } }
    public static uint GetSecretStatus(string name) { var attributes = new LsaObjectAttributes { Length = Marshal.SizeOf(typeof(LsaObjectAttributes)) }; IntPtr policy; var status = LsaOpenPolicy(IntPtr.Zero, ref attributes, PolicyGetPrivateInformation, out policy); if (status != 0) return status; var key = MakeString(name); IntPtr secret = IntPtr.Zero; try { return LsaRetrievePrivateData(policy, ref key, out secret); } finally { if (secret != IntPtr.Zero) LsaFreeMemory(secret); Marshal.FreeHGlobal(key.Buffer); LsaClose(policy); } }
}
'@
if ($null -eq ('AcikSystemAutoLogonLsa' -as [type])) { Add-Type -TypeDefinition $lsaSource -ErrorAction Stop }
$objectNotFound = [Convert]::ToUInt32('C0000034', 16)
$deleteStatus = [AcikSystemAutoLogonLsa]::DeleteSecret('DefaultPassword')
if ($deleteStatus -ne 0 -and $deleteStatus -ne $objectNotFound) { throw "Guvenli AutoLogon saklamasi silinemedi (LSA durum: $deleteStatus)." }
$verifyStatus = [AcikSystemAutoLogonLsa]::GetSecretStatus('DefaultPassword')
if ($verifyStatus -ne $objectNotFound) { throw "Guvenli AutoLogon saklamasi kaldigi icin hedef kullaniciya gecis durduruldu (LSA durum: $verifyStatus)." }
""".strip()

    def request_final_restart_after_x_cleanup(self, log: Logger) -> None:
        """Request the final restart only after verified X removal."""
        self._run(["shutdown.exe", "/r", "/t", "15", "/f"], log)
        log("X kullanicisi silindi; son yeniden baslatma 15 saniye icinde baslatilacak.")

    def schedule_x_cleanup_before_reboot(
        self,
        request: OnboardingRequest | None,
        log: Logger,
        *,
        run_id: str = "",
        target_username: str = "",
        cleanup_username: str = "",
        local_admin_username: str = "",
    ) -> str:
        """Register the final SYSTEM X cleanup; start it after reporting.

        If the operator is logged in as X, a normal elevated process cannot
        remove the loaded profile.  The task is deliberately registered first
        and started only after the GUI has durably written its report.  It then
        logs off X, removes the account and profile, verifies both
        postconditions, and only then restarts the computer.
        """
        if not self.is_admin_session():
            raise RuntimeError("x kullanicisi temizligi icin yonetici yetkisi gerekli.")

        # Recovery can be launched from the sealed post-login bundle, which
        # deliberately does not carry the USB's private configuration.  Use
        # the values captured in the signed workflow state when supplied, so
        # an elevated recovery never falls back to a different package's
        # defaults for the destructive X-cleanup target.
        cleanup_user = cleanup_username.strip() or self.config.legacy_cleanup_user.strip()
        local_admin = local_admin_username.strip() or self.config.tools.local_admin_username.strip()
        effective_target = target_username.strip() or (
            request.username.strip() if request is not None else ""
        )
        if not effective_target:
            raise RuntimeError("X temizligi icin hedef kullanici bilgisi eksik.")
        protected = {effective_target.casefold(), local_admin.casefold(), "administrator"}
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", cleanup_user):
            raise RuntimeError("Silinecek eski kullanici adi guvenli degil.")
        if cleanup_user.casefold() in protected:
            raise RuntimeError("Silinecek eski kullanici hedef veya lokaladm ile ayni olamaz.")

        effective_run_id = run_id.strip() or (
            request.run_id.strip() if request is not None else ""
        ) or uuid.uuid4().hex
        if request is not None:
            request.run_id = effective_run_id
        safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "", effective_run_id)[:32] or uuid.uuid4().hex[:12]
        runtime_dir = self.system_runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        script_path = runtime_dir / f"x_cleanup_{safe_run_id}.ps1"
        log_path = runtime_dir / f"x_cleanup_{safe_run_id}.log"
        portable_log_path = ""
        try:
            portable_log_dir = self.config.base_dir / "logs"
            portable_log_dir.mkdir(parents=True, exist_ok=True)
            portable_log_path = str(portable_log_dir / f"x_cleanup_{safe_run_id}.log")
        except OSError:
            # USB yazilamaz durumdaysa SYSTEM gunlugu yine ProgramData altinda kalir.
            pass
        task_name = f"AcikOnboardingXCleanup-{safe_run_id[:12]}"

        # Keep an audit record at registration time.  If Windows does not
        # launch the SYSTEM task, the USB log still proves whether the task
        # was registered or actually began execution.
        registration_line = (
            f"[{self.now_stamp()}] X cleanup task registered for {cleanup_user}; "
            f"waiting for final report. Task: {task_name}\n"
        )
        audit_paths = [log_path]
        if portable_log_path:
            audit_paths.append(Path(portable_log_path))
        for audit_path in audit_paths:
            try:
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                with audit_path.open("a", encoding="utf-8") as audit_log:
                    audit_log.write(registration_line)
            except OSError:
                # A write-protected USB must not stop the SYSTEM cleanup.
                pass

        cleanup_script = """param(
    [Parameter(Mandatory = $true)][string]$UserName,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [string]$PortableLogPath = '',
    [Parameter(Mandatory = $true)][string]$TaskName
)
$ErrorActionPreference = 'Stop'

function Write-CleanupLog([string]$Message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$stamp] $Message"
    # A damaged ProgramData ACL must not prevent the SYSTEM task from
    # continuing or from recording its result on the removable diagnostics
    # medium. Both audit destinations are independently best-effort.
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch {}
    if ($PortableLogPath) {
        try { Add-Content -LiteralPath $PortableLogPath -Value $line -Encoding UTF8 } catch {}
    }
}

function Remove-SafeProfileTree([string]$Path) {
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($item.PSIsContainer) {
                [IO.Directory]::Delete($item.FullName, $false)
            } else {
                Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
            }
        } elseif ($item.PSIsContainer) {
            Remove-SafeProfileTree $item.FullName
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
        } else {
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
        }
    }
}

try {
    Write-CleanupLog "SYSTEM task started for $UserName."
    # The main process writes its final report immediately after scheduling
    # this task. Give it time to finish before X is logged off.
    Start-Sleep -Seconds 12
    # Some images cannot import LocalAccounts from a SYSTEM task.  Continue
    # with net.exe rather than abandoning an explicitly requested X removal.
    Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction SilentlyContinue
    Write-CleanupLog "X cleanup started for $UserName."

    $sessionIds = @()
    $sessionLines = @(& query.exe user $UserName 2>&1)
    foreach ($line in $sessionLines) {
        if ($line -match ('^\\s*>?' + [regex]::Escape($UserName) + '\\s+(?<tail>.*)$')) {
            # query user leaves the session-name column blank for a
            # disconnected user. The first standalone number is its ID.
            $sessionId = @($Matches['tail'] -split '\\s+' | Where-Object { $_ -match '^\\d+$' } | Select-Object -First 1)
            if ($sessionId) {
                $sessionIds += $sessionId
            }
        }
    }

    # Some OEM images return an incomplete ``query user`` table for the
    # automatic console session.  The former working installer also resolved
    # X through its Explorer-process owner; retain that fallback without
    # loosening the account/profile deletion checks below.
    if ($sessionIds.Count -lt 1) {
        $explorers = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'explorer.exe'" -ErrorAction SilentlyContinue)
        foreach ($process in $explorers) {
            $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction SilentlyContinue
            if ($owner -and $owner.User -ieq $UserName) {
                $sessionIds += [string]$process.SessionId
            }
        }
    }
    $sessionIds = @($sessionIds | Where-Object { $_ -match '^\\d+$' } | Select-Object -Unique)
    foreach ($sessionId in $sessionIds) {
        Write-CleanupLog "Logging off session $sessionId for $UserName."
        & logoff.exe $sessionId
        if ($LASTEXITCODE -ne 0) { throw "logoff.exe failed for session $sessionId." }
    }

    # Retain the proven X-removal sequence: give logoff time to unload, then
    # stop any remaining X-owned processes before deleting its account/profile.
    Start-Sleep -Seconds 5
    $remainingProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)
    foreach ($process in $remainingProcesses) {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction SilentlyContinue
        if ($owner -and $owner.User -ieq $UserName) {
            Write-CleanupLog "Stopping remaining X process $($process.ProcessId)."
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 3

    $hasLocalAccountsCmdlet = [bool](Get-Command Get-LocalUser -ErrorAction SilentlyContinue)
    $user = if ($hasLocalAccountsCmdlet) { Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue } else { $null }
    $sid = if ($user) { $user.SID.Value } else { '' }
    $profile = $null
    try {
        $profile = Get-CimInstance Win32_UserProfile -ErrorAction Stop |
            Where-Object { ($sid -and $_.SID -eq $sid) -or $_.LocalPath -ieq ('C:\\Users\\' + $UserName) } |
            Select-Object -First 1
    } catch {
        Write-CleanupLog ("Win32_UserProfile lookup failed; safe path fallback will be used: " + $_.Exception.Message)
    }
    $profilePath = if ($profile) { [IO.Path]::GetFullPath($profile.LocalPath) } else { [IO.Path]::GetFullPath('C:\\Users\\' + $UserName) }
    $expectedRoot = [IO.Path]::GetFullPath('C:\\Users') + [IO.Path]::DirectorySeparatorChar
    if (-not $profilePath.StartsWith($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Profile path is outside C:\\Users: $profilePath"
    }

    for ($attempt = 1; $attempt -le 20 -and $profile -and $profile.Loaded; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            $profile = Get-CimInstance Win32_UserProfile -ErrorAction Stop |
                Where-Object { ($sid -and $_.SID -eq $sid) -or $_.LocalPath -ieq $profilePath } |
                Select-Object -First 1
        } catch {
            Write-CleanupLog ("Profile reload check failed; continuing with safe path verification: " + $_.Exception.Message)
            $profile = $null
        }
    }
    if ($profile -and $profile.Loaded) { throw 'Legacy profile is still loaded after logoff.' }

    # Delete the account first, exactly as the known-good legacy deployment
    # did.  If the cmdlet is unavailable or rejects SYSTEM, net.exe is the
    # mandatory fallback instead of a best-effort warning.
    $accountRemoved = if ($hasLocalAccountsCmdlet) { -not $user } else { $false }
    if ($user -and $hasLocalAccountsCmdlet) {
        try {
            Remove-LocalUser -Name $UserName -ErrorAction Stop
            $accountRemoved = $true
        } catch {
            Write-CleanupLog ("Remove-LocalUser failed; using net.exe: " + $_.Exception.Message)
        }
    }
    if (-not $accountRemoved) {
        & net.exe user $UserName /delete 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Legacy account removal failed (net.exe code: $LASTEXITCODE)." }
    }
    if ($hasLocalAccountsCmdlet) {
        if (Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue) { throw 'Legacy account deletion could not be verified.' }
    } else {
        & net.exe user $UserName 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { throw 'Legacy account deletion could not be verified.' }
    }

    # Let the Windows profile provider clear ProfileList and ordinary profile
    # contents before the guarded reparse-point-safe file fallback.
    if ($profile) {
        try {
            Remove-CimInstance -InputObject $profile -ErrorAction Stop
            $profile = $null
        } catch {
            Write-CleanupLog ("Win32_UserProfile cleanup failed; guarded file fallback continues: " + $_.Exception.Message)
        }
    }

    $profileItem = Get-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue
    if ($profileItem -and ($profileItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Legacy profile root is a reparse point; deletion refused.'
    }
    if ($profileItem) {
        & takeown.exe /f $profilePath /d y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'takeown.exe failed for the legacy profile.' }
        $adminSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
        $adminGroup = $adminSid.Translate([System.Security.Principal.NTAccount]).Value
        & icacls.exe $profilePath /grant "$adminGroup`:(OI)(CI)F" /c | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'icacls.exe failed for the legacy profile.' }
        Remove-SafeProfileTree $profilePath
        Remove-Item -LiteralPath $profilePath -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $profilePath) { throw 'Legacy profile deletion could not be verified.' }

    # X is gone. Apply the official Windows policy for local-account
    # enumeration on a domain-joined PC. This is a required final condition:
    # a restart is not permitted if Windows would fall back to a domain-only
    # credential screen instead of showing the selected local account.
    $accountPickerPath = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System'
    $localUsersPolicyPath = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\System'
    New-Item -Path $accountPickerPath -Force | Out-Null
    New-Item -Path $localUsersPolicyPath -Force | Out-Null
    Set-ItemProperty -Path $accountPickerPath -Name 'DontDisplayLastUserName' -Type DWord -Value 0 -Force
    Set-ItemProperty -Path $accountPickerPath -Name 'HideFastUserSwitching' -Type DWord -Value 0 -Force
    Set-ItemProperty -Path $localUsersPolicyPath -Name 'EnumerateLocalUsers' -Type DWord -Value 1 -Force
    $accountPicker = Get-ItemProperty -LiteralPath $accountPickerPath -ErrorAction Stop
    $localUsers = Get-ItemProperty -LiteralPath $localUsersPolicyPath -ErrorAction Stop
    if ([int]$accountPicker.DontDisplayLastUserName -ne 0 -or [int]$accountPicker.HideFastUserSwitching -ne 0 -or [int]$localUsers.EnumerateLocalUsers -ne 1) {
        throw 'Local account picker policy verification failed.'
    }
    Write-CleanupLog "Local account picker and local-user enumeration verified after X deletion."

    Write-CleanupLog "X cleanup verified. Removing cleanup task and restarting now."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    & shutdown.exe /r /t 0 /f
    exit 0
} catch {
    Write-CleanupLog ("X cleanup failed: " + $_.Exception.Message)
    exit 1
}
"""
        script_path.write_text(cleanup_script, encoding="utf-8")
        arguments = subprocess.list2cmdline(
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-UserName",
                cleanup_user,
                "-LogPath",
                str(log_path),
                "-PortableLogPath",
                portable_log_path,
                "-TaskName",
                task_name,
            ]
        )
        registration = f"""
$ErrorActionPreference = 'Stop'
$scriptPath = '{self.ps_escape(str(script_path))}'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {{ throw 'x temizleme betigi olusturulamadi.' }}
$acl = Get-Acl -LiteralPath $scriptPath
$acl.SetAccessRuleProtection($true, $false)
$systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$administratorsSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$acl.SetOwner($administratorsSid)
$acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($systemSid, 'FullControl', 'Allow')))
$acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($administratorsSid, 'FullControl', 'Allow')))
Set-Acl -LiteralPath $scriptPath -AclObject $acl
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{self.ps_escape(arguments)}'
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(90))
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if (-not (Get-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -ErrorAction SilentlyContinue)) {{
    throw 'X temizleme gorevi kaydedildikten sonra dogrulanamadi.'
}}
"""
        self.run_powershell(registration, log)
        log(f"x kullanicisi temizleme gorevi rapor sonrasi calismak uzere kaydedildi. Gunluk: {log_path}")
        return task_name

    def start_x_cleanup_after_report(self, task_name: str, log: Logger) -> None:
        """Start the already-registered SYSTEM cleanup only after reporting.

        This prevents an X-session logoff from interrupting the main process
        while it is still flushing the run report or USB diagnostics.
        """
        if not re.fullmatch(r"AcikOnboardingXCleanup-[A-Za-z0-9_-]{4,48}", task_name):
            raise RuntimeError("X temizleme gorev adi gecersiz.")
        # Preserve V5.7's known-good handoff: Task Scheduler owns the SYSTEM
        # transition after the durable report is written.  Waiting in the GUI
        # for its audit line caused a false failure on devices where the
        # scheduler starts the service task after the UI's 40-second wait.
        # The cleanup script remains the source of truth: it logs the
        # account/profile verification and requests the final reboot only then.
        script = f"""
$ErrorActionPreference = 'Stop'
$taskName = '{self.ps_escape(task_name)}'
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {{
    throw 'X temizleme gorevi bulunamadi.'
}}
Start-ScheduledTask -TaskName $taskName
"""
        self.run_powershell(script, log)
        log(f"Raporlar tamamlandi; V5.7 zinciriyle SYSTEM X temizleme gorevi baslatma komutu gonderildi: {task_name}")

    def schedule_x_cleanup_handoff_watchdog(
        self,
        run_id: str,
        cleanup_task_name: str,
        target_user_type: str,
        log: Logger,
    ) -> str:
        """Recover only when the already-started V5.7 SYSTEM cleanup never runs.

        The primary path remains the locked V5.7 sequence: register and start
        the SYSTEM X remover, let it verify account/profile/ProfileList, then
        reboot.  A few prepared images acknowledge ``Start-ScheduledTask``
        but never launch that task.  Leaving those devices on X blocks both
        the new user and the durable finalizer forever.  This independent
        watchdog never removes X.  It acts only when the cleanup has produced
        no start audit at all: it records recovery in the protected state,
        restores the documented local-account picker for local handoffs, and
        restarts so the existing SYSTEM finalizer can complete the verified
        deletion from the intended target session.
        """
        if not self.is_admin_session():
            raise RuntimeError("X temizleme watchdog gorevi icin yonetici yetkisi gerekli.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            raise RuntimeError("X temizleme watchdog run_id degeri gecersiz.")
        if not re.fullmatch(r"AcikOnboardingXCleanup-[A-Za-z0-9_-]{4,48}", cleanup_task_name):
            raise RuntimeError("X temizleme watchdog gorev adi gecersiz.")

        safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:32]
        runtime_dir = self.system_runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        script_path = runtime_dir / f"x_handoff_watchdog_{safe_run_id}.ps1"
        audit_path = runtime_dir / f"x_handoff_watchdog_{safe_run_id}.log"
        cleanup_log_path = runtime_dir / f"x_cleanup_{safe_run_id}.log"
        task_name = f"AcikOnboardingXHandoffWatchdog-{safe_run_id[:12]}"
        local_handoff = target_user_type.strip().casefold() == "lokal"
        script_path.write_text(
            f"""param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][string]$CleanupTaskName,
    [Parameter(Mandatory = $true)][string]$CleanupLogPath,
    [Parameter(Mandatory = $true)][string]$StatePath,
    [Parameter(Mandatory = $true)][string]$AuditPath,
    [Parameter(Mandatory = $true)][string]$RunId,
    [Parameter(Mandatory = $true)][bool]$LocalHandoff
)
$ErrorActionPreference = 'Stop'
function Write-WatchdogLog([string]$Message) {{
    try {{ Add-Content -LiteralPath $AuditPath -Value "$(Get-Date -Format o) $Message" -Encoding UTF8 }} catch {{}}
}}
try {{
    Write-WatchdogLog 'X handoff watchdog started.'
    $cleanupStarted = $false
    if (Test-Path -LiteralPath $CleanupLogPath -PathType Leaf) {{
        $cleanupStarted = [bool](Select-String -LiteralPath $CleanupLogPath -SimpleMatch 'SYSTEM task started for' -Quiet -ErrorAction SilentlyContinue)
    }}
    $cleanupTask = Get-ScheduledTask -TaskName $CleanupTaskName -ErrorAction SilentlyContinue
    if ($cleanupStarted -or ($cleanupTask -and $cleanupTask.State -eq 'Running')) {{
        Write-WatchdogLog 'Primary V5.7 X cleanup started; recovery restart is not needed.'
        exit 0
    }}

    # The original cleanup did not begin.  Keep the durable workflow and mark
    # only the recovery handoff, so the target-session SYSTEM finalizer owns
    # the next verified X deletion and final reboot.
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {{
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ([string]$state.run_id -eq $RunId) {{
            $state.immediate_x_cleanup = $false
            $state | Add-Member -NotePropertyName 'x_cleanup_recovery_handoff' -NotePropertyValue $true -Force
            $temporaryState = "$StatePath.$PID.tmp"
            [IO.File]::WriteAllText($temporaryState, ($state | ConvertTo-Json -Depth 16), [Text.UTF8Encoding]::new($false))
            Move-Item -LiteralPath $temporaryState -Destination $StatePath -Force
        }}
    }}
    if ($LocalHandoff) {{
        $signInPath = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System'
        $localUsersPath = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\System'
        New-Item -Path $signInPath -Force | Out-Null
        New-Item -Path $localUsersPath -Force | Out-Null
        Set-ItemProperty -Path $signInPath -Name 'DontDisplayLastUserName' -Type DWord -Value 0 -Force
        Set-ItemProperty -Path $signInPath -Name 'HideFastUserSwitching' -Type DWord -Value 0 -Force
        Set-ItemProperty -Path $localUsersPath -Name 'EnumerateLocalUsers' -Type DWord -Value 1 -Force
        $signIn = Get-ItemProperty -LiteralPath $signInPath -ErrorAction Stop
        $localUsers = Get-ItemProperty -LiteralPath $localUsersPath -ErrorAction Stop
        if ([int]$signIn.DontDisplayLastUserName -ne 0 -or [int]$signIn.HideFastUserSwitching -ne 0 -or [int]$localUsers.EnumerateLocalUsers -ne 1) {{
            throw 'Yerel kullanici secim ekrani ilkesi watchdog tarafindan dogrulanamadi.'
        }}
    }}
    Write-WatchdogLog 'Primary X cleanup did not start; target-session recovery restart is being requested.'
    & shutdown.exe /r /t 0 /f
    if ($LASTEXITCODE -ne 0) {{ Restart-Computer -Force -ErrorAction Stop }}
}} catch {{
    Write-WatchdogLog ("X handoff watchdog failed: " + $_.Exception.Message)
    exit 1
}} finally {{
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}}
""",
            encoding="utf-8",
        )
        arguments = subprocess.list2cmdline(
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-TaskName",
                task_name,
                "-CleanupTaskName",
                cleanup_task_name,
                "-CleanupLogPath",
                str(cleanup_log_path),
                "-StatePath",
                str(self.post_login_state_path()),
                "-AuditPath",
                str(audit_path),
                "-RunId",
                run_id,
                "-LocalHandoff",
                "$true" if local_handoff else "$false",
            ]
        )
        registration = f"""
$ErrorActionPreference = 'Stop'
$scriptPath = '{self.ps_escape(str(script_path))}'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {{ throw 'X handoff watchdog betigi olusturulamadi.' }}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{self.ps_escape(arguments)}'
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(150))
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if (-not (Get-ScheduledTask -TaskName '{self.ps_escape(task_name)}' -ErrorAction SilentlyContinue)) {{
    throw 'X handoff watchdog gorevi kaydedildikten sonra dogrulanamadi.'
}}
"""
        self.run_powershell(registration, log)
        log(
            "X temizleme watchdog gorevi kaydedildi; V5.7 SYSTEM gorevi baslamazsa "
            "150 saniye sonra hedef oturum handoff yeniden baslatmasi yapacak: " + task_name
        )
        return task_name

    def execute_system_finalize_tasks(self, state: dict[str, object], log: Logger) -> list[UiMessage]:
        messages: list[UiMessage] = []
        principal = str(state.get("target_principal", "")).strip()
        self._run_state_task(
            state,
            "eset",
            lambda: self.install_eset_from_state(state, log),
            "ESET kurulumu dogrulandi.",
            log,
            messages,
        )
        self._run_state_task(
            state,
            "lock_screen",
            lambda: self.apply_lock_screen_policy(state, log),
            "Kurumsal kilit ekrani ilkesi uygulandi.",
            log,
            messages,
        )
        self._run_state_task(
            state,
            "local_wallpaper_lock",
            lambda: self.apply_wallpaper_lock_policy(state, log),
            "Yerel standart kullanici icin sabit masaustu arka plani dogrulandi.",
            log,
            messages,
        )
        self._run_state_task(
            state,
            "grant_ip_admin",
            lambda: self.add_user_to_group(principal, "Network Configuration Operators", log),
            "IP degistirme yetkisi dogrulandi.",
            log,
            messages,
        )
        self._run_state_task(
            state,
            "grant_administrator",
            lambda: self.add_user_to_group(principal, "Administrators", log),
            "Kalici yonetici yetkisi dogrulandi.",
            log,
            messages,
        )
        tasks = state.get("tasks", {})
        delete_task = tasks.get("delete_x_user", {}) if isinstance(tasks, dict) else {}
        if (
            isinstance(delete_task, dict)
            and bool(delete_task.get("enabled"))
        ):
            # Kullanıcı fazındaki isteğe bağlı bir hata x temizliğini kalıcı olarak
            # atlamamalı. Bu işlem kendi güvenlik kontrolleri ve silme sonrası
            # doğrulaması ile yürütülür; aktif/hedef hesaplar yine silinemez.
            for attempt in range(1, 4):
                self._run_state_task(
                    state,
                    "delete_x_user",
                    lambda: self.delete_legacy_user_verified(state, log),
                    "Eski kullanici ve profili temizlendi.",
                    log,
                    messages,
                )
                refreshed_tasks = state.get("tasks", {})
                refreshed = refreshed_tasks.get("delete_x_user", {}) if isinstance(refreshed_tasks, dict) else {}
                if not isinstance(refreshed, dict) or str(refreshed.get("status", "")) != TASK_RETRYABLE_FAILED:
                    break
                log(f"X kullanicisi temizligi yeniden denenecek ({attempt}/3).")
                time.sleep(5)
        return messages

    def handle_system_finalize(self, run_id: str, log: Logger) -> list[UiMessage]:
        if not self.is_admin_session():
            raise RuntimeError("SYSTEM finalizasyonu yukseltilmis yetki olmadan calistirilamaz.")
        state = self.load_workflow_state()
        if str(state.get("run_id", "")) != run_id:
            raise RuntimeError("Finalizasyon run_id degeri bekleyen is akisi ile uyusmuyor.")

        # The periodic SYSTEM trigger can also fire while the original setup
        # account is still logged in (for example after a manually delayed
        # reboot).  Never run privileged finalization, especially X removal,
        # until the intended account is the active Windows session.
        interactive_identity = self.get_interactive_username(log).strip()
        interactive_username = interactive_identity.rsplit("\\", 1)[-1].casefold()
        target_username = str(state.get("target_username", "")).strip().casefold()
        target_principal = str(state.get("target_principal", "")).strip()
        target_user_type = str(state.get("target_user_type", "")).strip().casefold()
        if target_user_type == "domain":
            # This task intentionally wakes for every logon before a fresh
            # domain join has an active trust.  Compare the full DOMAIN\\user
            # identity so a same-named local account cannot receive domain
            # finalization or trigger the X-cleanup path.
            interactive_matches_target = bool(
                interactive_identity
                and target_principal
                and interactive_identity.casefold() == target_principal.casefold()
            )
        else:
            interactive_matches_target = bool(
                interactive_username and interactive_username == target_username
            )
        if not interactive_matches_target:
            tasks = state.get("tasks", {})
            delete_task = tasks.get("delete_x_user", {}) if isinstance(tasks, dict) else {}
            cleanup_user = str(
                state.get("legacy_cleanup_user", self.config.legacy_cleanup_user)
            ).strip().casefold()
            if (
                interactive_username
                and cleanup_user
                and interactive_username == cleanup_user
                and isinstance(delete_task, dict)
                and bool(delete_task.get("enabled"))
            ):
                self.logoff_legacy_session_for_target_handoff(state, log)
                return []
            log(
                "SYSTEM finalizasyonu hedef oturumu bekliyor; "
                f"mevcut kimlik: {interactive_identity or 'yok'}."
            )
            return []

        expires_at = str(state.get("expires_at", "")).strip()
        if expires_at:
            try:
                expired = datetime.now().astimezone() > datetime.fromisoformat(expires_at)
            except ValueError:
                expired = True
            if expired:
                tasks = state.get("tasks", {})
                if isinstance(tasks, dict):
                    for task_name, task in tasks.items():
                        if (
                            isinstance(task, dict)
                            and bool(task.get("enabled"))
                            and str(task.get("status", "")) not in {
                                TASK_SUCCEEDED,
                                TASK_SKIPPED,
                                TASK_PERMANENT_FAILED,
                            }
                        ):
                            mark_task(
                                state,
                                task_name,
                                TASK_PERMANENT_FAILED,
                                "Is akisi 48 saatlik zaman asimina ugradi.",
                            )
                self._set_phase_state(state, "user")
                self._set_phase_state(state, "system")
                self.write_workflow_state(state, log)
                messages = [
                    (
                        "Hata",
                        "Bekleyen kurulum 48 saat icinde tamamlanamadi ve guvenli sekilde durduruldu.",
                        "error",
                    )
                ]
                self._write_post_login_result(state, messages)
                task_name = str(state.get("system_task_name", ""))
                user_task_name = str(state.get("user_task_name", ""))
                self.clear_post_login_helper()
                self.clear_user_phase_artifacts(run_id)
                self.clear_post_login_state()
                self.remove_system_finalize_task(task_name, log)
                self.remove_user_phase_task(user_task_name, log)
                return messages

        if enabled_phase_tasks(state, "user"):
            if not str(state.get("target_sid", "")).strip():
                resolved_sid = self.resolve_account_sid(
                    str(state.get("target_principal", "")),
                    log,
                )
                if resolved_sid:
                    state["target_sid"] = resolved_sid
                    self.write_workflow_state(state, log)
                    self.write_user_phase_plan(state, log)
            acl_ready = self._protect_user_phase_dir(state, log, grant_target=True)
            progress_path = self.user_phase_progress_path(run_id)
            if acl_ready and progress_path.exists():
                try:
                    self.merge_user_phase_progress(state, read_json(progress_path))
                    self.write_workflow_state(state, log)
                except Exception as exc:
                    log(f"Kullanici ilerleme dosyasi birlestirilemedi: {exc}")

            # Do not convert unfinished user work into failure merely because
            # the two AtLogOn tasks started in a different order.  The durable
            # retry trigger will call this method again after the user task has
            # written progress, including after a manual reboot.
            if phase_status(state, "user") not in {"completed", "partial", "not_required"}:
                log(
                    "Kullanici fazi henuz tamamlanmadi; SYSTEM finalizasyonu "
                    "beklemeden cikiyor ve iki dakika sonra yeniden deneyecek."
                )
                self._set_phase_state(state, "user")
                self.write_workflow_state(state, log)
                self._update_report_from_state(state, log)
                return []

        log(f"SYSTEM finalizasyonu baslatildi. Run ID: {run_id}")
        messages = [
            tuple(message)
            for message in state.get("user_messages", [])
            if isinstance(message, list) and len(message) == 3
        ]
        messages.extend(self.execute_system_finalize_tasks(state, log))
        # Do NOT call finalize_retryable_phase_tasks here. execute_system_finalize_tasks
        # already ran every enabled task exactly once through _run_state_task, which
        # marks a failure TASK_RETRYABLE_FAILED until it has failed 3 times
        # (see the "Gorev basarisiz (attempts/3)" log line), so multi-pass retries
        # across the periodic 2-minute SYSTEM trigger are the intended recovery path.
        # Calling finalize_retryable_phase_tasks on every pass promoted a task to
        # TASK_PERMANENT_FAILED after its very first failure, which defeated that
        # retry design. The already-correct 48-hour expiry block above (which marks
        # every unfinished enabled task TASK_PERMANENT_FAILED once the workflow is
        # truly out of time) remains the only place non-critical SYSTEM tasks are
        # force-terminated. delete_x_user still gets its own in-pass 3x retry loop
        # inside execute_system_finalize_tasks and is only forced terminal by the
        # 48-hour expiry block, never by this removed call.
        self._set_phase_state(state, "system")
        self.write_workflow_state(state, log)
        self._update_report_from_state(state, log)
        status = workflow_status(state)
        tasks = state.get("tasks", {})
        delete_task = tasks.get("delete_x_user", {}) if isinstance(tasks, dict) else {}
        x_cleanup_completed = (
            isinstance(delete_task, dict)
            and bool(delete_task.get("enabled"))
            and str(delete_task.get("status", "")) == TASK_SUCCEEDED
        )
        immediate_x_cleanup = bool(state.get("immediate_x_cleanup"))
        if status in {"completed", "partial"}:
            if x_cleanup_completed:
                messages.append(
                    (
                        "Bilgi",
                        (
                            "X kullanicisi ve profili dogrulandi; SYSTEM temizligi "
                            "zaten dogrulama sonrasi yeniden baslatma yapti."
                            if immediate_x_cleanup
                            else "X kullanicisi ve profili dogrulandi. Son yeniden baslatma planlaniyor."
                        ),
                        "info",
                    )
                )
            self._write_post_login_result(state, messages)
            task_name = str(state.get("system_task_name", ""))
            user_task_name = str(state.get("user_task_name", ""))
            self.clear_post_login_helper()
            self.clear_user_phase_artifacts(run_id)
            self.clear_post_login_state()
            self.remove_system_finalize_task(task_name, log)
            self.remove_user_phase_task(user_task_name, log)
            if x_cleanup_completed and not immediate_x_cleanup:
                # All reports, state cleanup, and removal verification have
                # completed. The final reboot is intentionally the last
                # operational action in the workflow.
                self.request_final_restart_after_x_cleanup(log)
        log(f"SYSTEM finalizasyon durumu: {status}")
        return messages

    def apply_onboarding(self, request: OnboardingRequest, log: Logger) -> list[UiMessage]:
        """Ana kurulum akisini adim adim yurutur ve raporlar.

        Stajyer Notu:
        - Bu fonksiyon UI'dan gelen istek paketini alir.
        - Secili checkbox/toggle durumlarina gore hangi adimlarin kosacagina karar verir.
        - Tum log ve rapor kayitlari bu merkez akis uzerinden akar.
        """
        if not request.run_id:
            request.run_id = uuid.uuid4().hex
        self.validate_request(request)
        self.assert_no_active_workflow(log)
        self.run_preflight(request, log)
        preflight_errors = self.preflight_errors_for(request)
        skip_preflight_once = bool(request.options.pop("_skip_preflight_once", False))
        if preflight_errors and not skip_preflight_once:
            raise RuntimeError("On kontrol basarisiz:\n- " + "\n- ".join(preflight_errors))
        if preflight_errors:
            log("On kontrol uyarilari operator istegiyle atlandi; her kurulum adimi kendi dogrulamasini yapacak.")
            for error in preflight_errors:
                log(f"Atlanan on kontrol uyarisi: {error}")

        messages: list[UiMessage] = []
        if preflight_errors:
            messages.append(("Uyari", "On kontrol uyarilari operator istegiyle atlandi.", "warning"))
        options = request.options
        started_at = datetime.now()
        inventory = self.get_system_inventory(log)
        steps: list[StepResult] = []
        noncritical_failures: list[str] = []
        has_deferred_work = False
        restart_required = False
        x_cleanup_task_name = ""

        report_payload: dict[str, object] = {
            "run_id": request.run_id,
            "run_started_at": started_at.isoformat(timespec="seconds"),
            "status": "running",
            "profile_name": request.profile_name,
            "company_name": request.company_name,
            "user_type": request.user_type,
            "full_name": request.full_name,
            "username": request.username,
            "computer_name": request.computer_name,
            "selected_options": options,
            "inventory": inventory,
            "steps": [],
        }

        def run_step(name: str, callback: Callable[[], None], success_detail: str, critical: bool = False) -> None:
            step_started = datetime.now()
            log(f"Adim basladi: {name}")
            try:
                callback()
                steps.append(self.create_step_result(name, "ok", success_detail, step_started))
                log(f"Adim tamamlandi: {name}")
            except Exception as exc:
                if critical:
                    steps.append(self.create_step_result(name, "error", str(exc), step_started))
                    log(f"Kritik adim basarisiz oldu: {name}. Hata: {exc}")
                    raise
                else:
                    log(f"Adim basarisiz: {name} - Hata: {exc}")
                    noncritical_failures.append(name)
                    steps.append(self.create_step_result(name, "error", str(exc), step_started))

        try:
            log("Kurulum akisi basladi.")

            if options.get("rename_admin"):
                run_step("Lokaladm", lambda: self.prepare_local_admin(log), "Lokaladm hesabi dogrulandi.", critical=True)
            elif options.get("delete_x_user"):
                raise RuntimeError("x kullanicisini silmek icin once Gecici Admin adimi aktif olmali.")

            if request.user_type == "Lokal":
                run_step(
                    "Yerel kullanici",
                    lambda: self.create_or_update_local_user(request.full_name, request.username, request.password, log),
                    f"{request.username} kullanicisi hazirlandi.",
                    critical=True,
                )
                if options.get("ip_admin"):
                    run_step(
                        "IP Admin",
                        lambda: self.add_user_to_group(request.username, "Network Configuration Operators", log),
                        "Network Configuration Operators yetkisi verildi.",
                        critical=False,
                    )
                if options.get("administrator"):
                    run_step(
                        "Administrator",
                        lambda: self.add_user_to_group(request.username, "Administrators", log),
                        "Administrators yetkisi verildi.",
                        critical=False,
                    )
                run_step(
                    "Bilgisayar adi",
                    lambda: self.set_computer_name(request.computer_name, log),
                    "Bilgisayar adi akisi tamamlandi; gerekirse yeniden baslatma sonrasi etkinlesecek.",
                    critical=True,
                )
                messages.append(("Basarili", f"{request.username} kullanicisi hazirlandi.", "info"))
                messages.append(("Bilgi", "Bilgisayar adi degisimi icin yeniden baslatma gerekebilir.", "warning"))
            elif request.user_type == "Domain":
                run_step(
                    "Domain katilimi",
                    lambda: self.join_domain(
                        request.computer_name,
                        request.username,
                        request.password,
                        log,
                    ),
                    f"Domain katilim komutu gonderildi: {self.config.domain.name}",
                    critical=True,
                )
                if options.get("ip_admin"):
                    steps.append(
                        self.create_step_result(
                            "IP Admin",
                            "deferred",
                            "Domain yeniden baslatmasindan sonra SYSTEM finalizasyonuna ertelendi.",
                            datetime.now(),
                        )
                    )
                if options.get("administrator"):
                    steps.append(
                        self.create_step_result(
                            "Administrator",
                            "deferred",
                            "Domain yeniden baslatmasindan sonra SYSTEM finalizasyonuna ertelendi.",
                            datetime.now(),
                        )
                    )
                    
                messages.append(("Basarili", "Domain katilim komutu basariyla gonderildi.", "info"))
                messages.append(("Bilgi", "Domain katilimi sonrasinda yeniden baslatma gerekecek.", "warning"))
                domain_sign_in = self._target_principal(request.username, "Domain")
                messages.append(
                    (
                        "Bilgi",
                        "Yeniden baslatmadan sonra Windows girisinde Kullanici adi alanina "
                        f"{domain_sign_in} yazin. Parola ekranda veya gunlukte gosterilmez.",
                        "warning",
                    )
                )
            else:
                raise RuntimeError("Kullanici tipi secilmedi.")

            if options.get("anydesk"):
                run_step("AnyDesk", lambda: self.install_anydesk(log), "AnyDesk kurulumu calistirildi.")

            if options.get("wifi_sync"):
                wifi_profile = self.config.wifi_profiles.get("general")
                if not wifi_profile:
                    raise RuntimeError("Genel Wi-Fi profili ayarlanmamis.")
                run_step(
                    "Wi-Fi ve saat",
                    lambda: self.connect_wifi_and_sync_time(wifi_profile, log),
                    f"Wi-Fi baglantisi ve saat esitlemesi tamamlandi: {wifi_profile.ssid}",
                )

            if options.get("windows_activation"):
                run_step("Windows etkinlestirme", lambda: self.check_windows_activation(log), "Windows lisans kontrolu tamamlandi.")

            if options.get("eset"):
                # ESET kurulumu bekleyen yeniden başlatma hatasını (1603) önlemek için ikinci faza ertelenmiştir.
                # Rapor üzerinde doğru şekilde "atlandı/ertelendi (skipped)" görünmesi için doğrudan steps listesine ekliyoruz.
                steps.append(self.create_step_result(
                    "ESET",
                    "deferred",
                    "ESET kurulumu bekleyen yeniden baslatma (1603) hatasini onlemek icin ikinci faza (post-login) ertelendi.",
                    datetime.now()
                ))

            if options.get("hackbgrt"):
                run_step("HackBGRT", lambda: self.run_hackbgrt(log), "HackBGRT EFI dosyaları kopyalandı ve kurulum tetiklendi.")

            has_deferred_work = bool(
                options.get("main_file_server")
                or options.get("network_printer")
                or (
                    request.user_type == "Lokal"
                    and options.get("desktop_wallpaper")
                    and not options.get("administrator")
                )
                or options.get("desktop_signature")
                or options.get("classic_outlook")
                or options.get("eset")
                or options.get("windows_update")
                or options.get("delete_x_user")
                or (
                    request.user_type == "Domain"
                    and (options.get("ip_admin") or options.get("administrator"))
                )
            )
            if has_deferred_work:
                run_step(
                    "Ikinci faz gorevleri",
                    lambda: self.schedule_post_login_tasks(
                        request,
                        log,
                        immediate_x_cleanup=bool(options.get("delete_x_user")),
                    ),
                    "Yeni kullanici oturumu icin ikinci faz gorevleri planlandi.",
                    critical=True,
                )
                messages.append(("Bilgi", "File Server, yazici ve son kullanici gorevleri yeni oturuma planlandi.", "info"))

            if options.get("delete_x_user"):
                run_step(
                    "X otomatik giris kapatma",
                    lambda: self.disable_automatic_signin_for_target_handoff(log),
                    "V5.7 referans zinciriyle standart ve guvenli otomatik giris saklamalari temizlendi; X hesabini SYSTEM gorevi silecek.",
                    critical=True,
                )

                def register_x_cleanup() -> None:
                    nonlocal x_cleanup_task_name
                    x_cleanup_task_name = self.schedule_x_cleanup_before_reboot(
                        request,
                        log,
                    )

                run_step(
                    "x kullanicisi temizligi",
                    register_x_cleanup,
                    "SYSTEM temizleme gorevi rapor sonrasi baslatilmak uzere kaydedildi; X henuz silinmedi.",
                    critical=True,
                )
                messages.append(
                    (
                        "Bilgi",
                        "Raporlar yazildiktan sonra SYSTEM X oturumunu kapatacak; hesap ve profil silinip dogrulanmadan yeniden baslatma yapilmayacak.",
                        "warning",
                    )
                )

            restart_required = bool(
                request.user_type == "Domain"
                or options.get("restart")
                or has_deferred_work
            ) and not bool(x_cleanup_task_name)
            if restart_required:
                steps.append(
                    self.create_step_result(
                        "Yeniden baslat",
                        "deferred",
                        "Rapor ve is akisi yazildiktan sonra hedef kullanici oturumuna gecis icin otomatik yeniden baslatma istenecek.",
                        datetime.now(),
                    )
                )
                messages.append(("Bilgi", "Ikinci faz icin bilgisayar otomatik yeniden baslatilacak; yeni kullanici ile oturum acin.", "warning"))

            if options.get("delete_x_user"):
                report_payload["status"] = "awaiting_post_login"
            elif has_deferred_work:
                report_payload["status"] = "awaiting_post_login"
            elif noncritical_failures:
                report_payload["status"] = "partial"
            else:
                report_payload["status"] = "completed"
            if noncritical_failures:
                messages.append(
                    (
                        "Uyari",
                        "Bazi secili adimlar tamamlanamadi: " + ", ".join(noncritical_failures),
                        "warning",
                    )
                )
            log("Kurulum akisi tamamlandi.")
            return messages
        except Exception as exc:  # noqa: BLE001
            failed_started = datetime.now()
            steps.append(self.create_step_result("Akis sonlandi", "error", str(exc), failed_started))
            report_payload["status"] = "failed"
            report_payload["error"] = str(exc)
            raise
        finally:
            report_payload["run_finished_at"] = self.now_stamp()
            report_payload["steps"] = [asdict(step) for step in steps]
            report_payload["messages"] = messages
            try:
                report_path = self.write_run_report(report_payload, log)
                report_payload["report_path"] = str(report_path)
            except Exception as exc:  # noqa: BLE001
                log(f"Kurulum raporu yazilamadi: {exc}")
                report_payload["report_path"] = ""
            try:
                self.export_run_failure_to_usb(report_payload, log)
            except Exception as exc:  # noqa: BLE001
                log(f"USB hata kaydi hazirlanamadi: {exc}")
            try:
                self.dispatch_run_report(report_payload, log)
            except Exception as exc:  # noqa: BLE001
                log(f"Kurulum raporu dagitilamadi: {exc}")
            if x_cleanup_task_name and report_payload.get("status") != "failed":
                try:
                    self.start_x_cleanup_after_report(x_cleanup_task_name, log)
                    options["_x_cleanup_started_after_report"] = True
                    log("Raporlar tamamlandi; dogrulanmis SYSTEM X temizligi baslatildi.")
                    try:
                        watchdog_task = self.schedule_x_cleanup_handoff_watchdog(
                            request.run_id,
                            x_cleanup_task_name,
                            request.user_type,
                            log,
                        )
                        options["_x_cleanup_watchdog_task"] = watchdog_task
                    except Exception as watchdog_exc:  # noqa: BLE001 - baseline X cleanup stays primary
                        log(
                            "X temizleme watchdog gorevi kaydedilemedi; V5.7 SYSTEM temizleme gorevi "
                            f"ana yol olarak calismaya devam edecek: {watchdog_exc}"
                        )
                except Exception as exc:  # noqa: BLE001
                    log(f"SYSTEM X temizleme gorevi baslatilamadi: {exc}")
                    # A registered task that has not started must never be
                    # presented as a successful restart handoff. The state is
                    # intentionally retained for the administrator recovery UI.
                    raise RuntimeError(
                        "SYSTEM X temizleme gorevi baslatilamadigi icin yeniden baslatma onaylanmadi. "
                        "Bekleyen kurulum ekranindan yonetici kurtarmasini kullanin."
                    ) from exc
            elif restart_required and report_payload.get("status") != "failed":
                try:
                    restart_request = self.schedule_initial_restart_after_setup(request, log)
                    options["_initial_restart_scheduled"] = True
                    log(f"Otomatik yeniden baslatma rapor sonrasi dogrulandi: {restart_request}")
                except Exception as exc:  # noqa: BLE001
                    log(f"Otomatik yeniden baslatma istenemedi: {exc}")

    def backup_profile(self, username: str, destination_root: str, log: Logger) -> list[UiMessage]:
        if not username.strip():
            raise RuntimeError("Kullanici adi bos olamaz.")
        source_root = Path("C:/Users") / username
        return self.backup_profile_custom(str(source_root), destination_root, log)

    def backup_profile_custom(self, source_dir: str, destination_root: str, log: Logger) -> list[UiMessage]:
        source_path = Path(source_dir.strip()).resolve()
        target_root = destination_root.strip() or self.config.backup.network_path
        if not source_dir.strip() or not source_path.exists() or not source_path.is_dir():
            raise RuntimeError(f"Kaynak profil klasoru bulunamadi: {source_path}")
        if not target_root:
            raise RuntimeError("Yedek hedefi bos.")
        if target_root.startswith("\\\\") and self.config.backup.network_user and self.config.backup.network_password:
            parts = [part for part in target_root.split("\\") if part]
            if len(parts) < 2:
                raise RuntimeError("Ag yedek hedefi UNC formatinda degil.")
            share_root = f"\\\\{parts[0]}\\{parts[1]}"
            self.connect_network_resource(
                share_root,
                self.config.backup.network_user,
                self.config.backup.network_password,
                log,
            )

        destination_base = (Path(target_root) / source_path.name).resolve()
        try:
            destination_base.relative_to(source_path)
        except ValueError:
            pass
        else:
            raise RuntimeError("Yedek hedefi kaynak profil klasorunun icinde olamaz.")
        destination_base.mkdir(parents=True, exist_ok=True)

        copied_files = 0
        copied_bytes = 0
        failures: list[str] = []
        excluded_names = {
            "appdata",
            "ntuser.dat",
            "ntuser.ini",
            "$recycle.bin",
        }

        def copy_tree(source: Path, destination: Path) -> None:
            nonlocal copied_files, copied_bytes
            destination.mkdir(parents=True, exist_ok=True)
            try:
                entries = list(source.iterdir())
            except OSError as exc:
                failures.append(f"{source}: {exc}")
                return
            for entry in entries:
                lowered = entry.name.casefold()
                if (
                    lowered in excluded_names
                    or lowered.startswith("ntuser.dat")
                    or lowered.startswith("usrclass.dat")
                    or entry.is_symlink()
                ):
                    continue
                target = destination / entry.name
                try:
                    if entry.is_dir():
                        copy_tree(entry, target)
                    elif entry.is_file():
                        shutil.copy2(entry, target)
                        source_size = entry.stat().st_size
                        if not target.exists() or target.stat().st_size != source_size:
                            raise OSError("Kopya boyutu dogrulanamadi.")
                        copied_files += 1
                        copied_bytes += source_size
                except OSError as exc:
                    failures.append(f"{entry}: {exc}")

        requested_folders = self.config.backup.folders or ["Desktop", "Documents", "Pictures", "Videos"]
        existing_folder_count = 0
        for folder_name in requested_folders:
            source_folder = source_path / folder_name
            if not source_folder.exists():
                log(f"Yedek klasoru bulunamadi, atlandi: {source_folder}")
                continue
            existing_folder_count += 1
            log(f"Yedekleniyor: {source_folder}")
            copy_tree(source_folder, destination_base / folder_name)

        if existing_folder_count == 0:
            raise RuntimeError("Yedeklenecek standart profil klasoru bulunamadi.")
        if failures:
            preview = "\n".join(failures[:8])
            raise RuntimeError(
                f"Yedekleme kismi tamamlandi ancak {len(failures)} dosya/klasor kopyalanamadi:\n{preview}"
            )
        log(f"Yedekleme dogrulandi: {copied_files} dosya, {copied_bytes} bayt.")
        return [
            (
                "Basarili",
                f"Profil yedekleme tamamlandi: {copied_files} dosya dogrulandi.",
                "info",
            )
        ]

    def detect_usb_util_path(self) -> Path | None:
        """Removable veya diger suruculer altinda 1.UTIL_KURULUM klasörünü arar."""
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            path = Path(f"{letter}:/1.UTIL_KURULUM")
            if path.exists() and path.is_dir():
                return path
        return None

    def is_program_installed(self, program_name: str) -> bool:
        """Programların yüklü olup olmadığını standart dosya yolları veya registry ile kontrol eder."""
        if program_name == "hackbgrt":
            completed = self._run_quiet(["bcdedit.exe", "/enum", "firmware"])
            return completed.returncode == 0 and "hackbgrt" in completed.stdout.casefold()
        paths = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
            ],
            "anydesk": [
                r"C:\Program Files (x86)\AnyDesk\AnyDesk.exe",
                r"C:\Program Files\AnyDesk\AnyDesk.exe"
            ],
            "forticlient": [
                r"C:\Program Files\Fortinet\FortiClient\FortiClient.exe",
                r"C:\Program Files (x86)\Fortinet\FortiClient\FortiClient.exe"
            ],
            "winrar": [
                r"C:\Program Files\WinRAR\WinRAR.exe"
            ],
            "eset": [
                r"C:\Program Files\ESET\ESET Security\ecmd.exe",
                r"C:\Program Files\ESET\Remote Administrator\Agent\ERAAgent.exe"
            ],
            "office": [
                r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE",
                r"C:\Program Files (x86)\Microsoft Office\root\Office16\OUTLOOK.EXE"
            ],
            "jre": [
                r"C:\Program Files\Java",
                r"C:\Program Files (x86)\Java"
            ]
        }

        if program_name in paths:
            for p in paths[program_name]:
                path_obj = Path(p)
                if path_obj.exists():
                    if program_name == "jre" and path_obj.is_dir() and any(path_obj.iterdir()):
                        return True
                    elif path_obj.is_file():
                        return True

        reg_keywords = {
            "chrome": "Google Chrome",
            "anydesk": "AnyDesk",
            "forticlient": "FortiClient",
            "winrar": "WinRAR",
            "eset": "ESET",
            "office": "Microsoft Office",
            "jre": "Java"
        }
        
        if program_name in reg_keywords:
            keyword = reg_keywords[program_name]
            try:
                import winreg
                hives = [
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
                ]
                for hive, subkey_path in hives:
                    try:
                        with winreg.OpenKey(hive, subkey_path) as key:
                            info = winreg.QueryInfoKey(key)
                            for i in range(info[0]):
                                try:
                                    subkey_name = winreg.EnumKey(key, i)
                                    with winreg.OpenKey(key, subkey_name) as subkey:
                                        try:
                                            display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                            if keyword.lower() in str(display_name).lower():
                                                return True
                                        except OSError:
                                            pass
                                except OSError:
                                    pass
                    except OSError:
                        pass
            except Exception:
                pass

        return False

    def delete_all_reports(self) -> list[str]:
        """Delete every stored report and return the names that could not be removed.

        Reports contain full names, usernames, computer names and hardware
        serial numbers, so an operator clearing them for privacy/decommission
        reasons must be told when something (a locked/permission-denied file)
        was left behind instead of that failure being swallowed silently.
        """
        report_dir = self.report_output_dir()
        failed: list[str] = []
        if report_dir.exists():
            import shutil
            for item in report_dir.iterdir():
                try:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                except Exception:
                    failed.append(item.name)
        return failed

    def _wait_for_program(self, program_name: str, timeout_seconds: int, log: Logger) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.is_program_installed(program_name):
                log(f"Kurulum dogrulandi: {program_name}")
                return True
            time.sleep(5)
        return False

    def _forticlient_config_executable(self) -> Path:
        """Return the supported FortiClient profile-import executable.

        FCConfig imports the encrypted pre-shared key without this application
        ever reading, printing, or persisting that secret itself.
        """
        candidates = (
            Path(r"C:\Program Files\Fortinet\FortiClient\FCConfig.exe"),
            Path(r"C:\Program Files (x86)\Fortinet\FortiClient\FCConfig.exe"),
        )
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            raise RuntimeError("FortiClient FCConfig.exe bulunamadi; FortiClient kurulumu tamamlanmamis olabilir.")
        return executable

    @staticmethod
    def _normalize_forticlient_login_name(full_name: str) -> str:
        """Validate the non-secret display name used for FortiClient XAuth."""
        normalized = " ".join(full_name.split())
        if not normalized:
            return ""
        if len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
            raise RuntimeError("FortiClient Save Login kullanici adi gecersiz.")
        return normalized

    def _forticlient_login_name_from_interactive_local_user(self, log: Logger) -> str:
        """Read the active local account's Windows ``FullName`` property.

        The value is deliberately not taken from the installer form or an
        onboarding report.  Those values describe a requested account and can
        be stale after the application is reopened.  Computer Management's
        ``Yerel Kullanıcılar ve Gruplar > Kullanıcılar > Tam ad`` is exposed by
        ``Get-LocalUser.FullName`` and is the only source accepted here.
        """
        interactive_identity = self.get_interactive_username(log).strip()
        if "\\" not in interactive_identity:
            return ""
        computer_scope, username = interactive_identity.rsplit("\\", 1)
        computer_name = os.environ.get("COMPUTERNAME", "").strip()
        if not computer_name or computer_scope.casefold() != computer_name.casefold():
            # Forti Save Login is intentionally local-only.  A domain session
            # must never be mapped to a similarly named local account.
            return ""
        if not username.strip():
            return ""

        command = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop; "
            f"$account = Get-LocalUser -Name '{self.ps_escape(username.strip())}' -ErrorAction Stop; "
            "[Console]::Out.Write([string]$account.FullName)"
        )
        completed = self._run_quiet(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            timeout_seconds=20,
        )
        if completed.returncode != 0:
            return ""
        return self._normalize_forticlient_login_name(completed.stdout)

    def _resolve_forticlient_save_login_name(self, log: Logger) -> str:
        full_name = self._forticlient_login_name_from_interactive_local_user(log)
        if full_name:
            return full_name
        raise RuntimeError(
            "FortiClient Save Login icin aktif yerel Windows kullanicisinin Bilgisayar Yonetimi "
            "ekranindaki Tam ad alani bulunamadi. Domain oturumunda bu ayar uygulanmaz."
        )

    @staticmethod
    def _forticlient_export_connection(tree: ElementTree.ElementTree) -> ElementTree.Element:
        root = tree.getroot()
        connections = root.findall("./vpn/ipsecvpn/connections/connection")
        connection = next(
            (
                item
                for item in connections
                if (item.findtext("name") or "").strip() == FORTICLIENT_VPN_CONNECTION_NAME
            ),
            None,
        )
        if connection is None:
            raise RuntimeError(
                f"FortiClient profili {FORTICLIENT_VPN_CONNECTION_NAME} IPsec baglantisini icermiyor."
            )
        return connection

    def _write_forticlient_save_login(self, export_path: Path, full_name: str) -> None:
        """Set only the selected tunnel's official Save Login fields.

        FortiClient 7.0 requires ``ui/save_username`` plus
        ``xauth/prompt_username=0`` before it retains an XAuth username.
        The exported configuration is the live client configuration, so the
        profile is not rebuilt and no VPN endpoint, password, or other
        administrator setting is changed.
        """
        try:
            tree = ElementTree.parse(export_path)
        except (ElementTree.ParseError, OSError) as exc:
            raise RuntimeError("FortiClient VPN yapilandirmasi okunamadi.") from exc

        connection = self._forticlient_export_connection(tree)
        ui = connection.find("ui")
        if ui is None:
            ui = ElementTree.SubElement(connection, "ui")
        save_username = ui.find("save_username")
        if save_username is None:
            save_username = ElementTree.SubElement(ui, "save_username")
        save_username.text = "1"

        xauth = connection.find("./ike_settings/xauth")
        if xauth is None:
            raise RuntimeError("FortiClient IPsec profilinde kullanici adi alani bulunamadi.")
        username = xauth.find("username")
        if username is None:
            username = ElementTree.SubElement(xauth, "username")
        username.text = full_name
        prompt_username = xauth.find("prompt_username")
        if prompt_username is None:
            prompt_username = ElementTree.SubElement(xauth, "prompt_username")
        prompt_username.text = "0"

        try:
            tree.write(export_path, encoding="utf-8", xml_declaration=True)
        except OSError as exc:
            raise RuntimeError("FortiClient Save Login yapilandirmasi yazilamadi.") from exc

    def _forticlient_export_has_saved_login(self, export_path: Path, full_name: str) -> bool:
        try:
            tree = ElementTree.parse(export_path)
            connection = self._forticlient_export_connection(tree)
        except (ElementTree.ParseError, OSError, RuntimeError):
            return False
        return (
            (connection.findtext("./ui/save_username") or "").strip() == "1"
            and (connection.findtext("./ike_settings/xauth/username") or "").strip() == full_name
            and (connection.findtext("./ike_settings/xauth/prompt_username") or "").strip() == "0"
        )

    def _restrict_forticlient_temp_directory(self, directory: Path) -> None:
        """Keep FCConfig's short-lived plaintext export readable by admins only."""
        result = self._run_quiet(
            [
                "icacls.exe",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)(F)",
                "*S-1-5-32-544:(OI)(CI)(F)",
            ],
            timeout_seconds=15,
        )
        if result.returncode != 0:
            raise RuntimeError("FortiClient gecici yapilandirma klasoru korunamadi.")

    @staticmethod
    def _forticlient_vpn_export_command(
        executable: Path,
        export_path: Path,
        export_password: str,
    ) -> list[str]:
        """Build the FortiClient 7.0 VPN-module export command.

        ``exportvpn`` returns success without producing a file in FortiClient
        7.0.14.  The documented VPN module ``export`` operation with an export
        password produces readable XML on that client.  The random password
        protects only the short-lived export and is never written or logged.
        """
        return [
            str(executable),
            "-m",
            "vpn",
            "-f",
            str(export_path),
            "-o",
            "export",
            "-p",
            export_password,
            "-q",
        ]

    @staticmethod
    def _forticlient_vpn_import_command(
        executable: Path,
        import_path: Path,
        export_password: str,
    ) -> list[str]:
        """Build the matching FortiClient 7.0 VPN-module import command."""
        return [
            str(executable),
            "-m",
            "vpn",
            "-f",
            str(import_path),
            "-o",
            "import",
            "-p",
            export_password,
            "-q",
        ]

    def _forticlient_live_vpn_has_tunnel(self, log: Logger) -> bool:
        """Return whether the live client really contains the target tunnel.

        The old ProgramData marker recorded a successful process exit code but
        did not prove that FCConfig had added the profile.  This method uses a
        short-lived, ACL-protected official VPN-module export and checks only
        for the target connection name.  No export content is logged.
        """
        executable = self._forticlient_config_executable()
        state_root = self._forticlient_profile_state_path().parent
        state_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix="forti-tunnel-check-", dir=state_root))
        export_path = temporary_root / "live-vpn.conf"
        export_password = f"acik-export-{uuid.uuid4().hex}"
        try:
            self._restrict_forticlient_temp_directory(temporary_root)
            result = self._run_quiet(
                self._forticlient_vpn_export_command(executable, export_path, export_password),
                timeout_seconds=90,
            )
            if not export_path.is_file():
                raise RuntimeError(
                    "FortiClient canli VPN yapilandirmasi okunamadi "
                    f"(FCConfig kodu {result.returncode})."
                )
            try:
                tree = ElementTree.parse(export_path)
                self._forticlient_export_connection(tree)
            except (ElementTree.ParseError, OSError, RuntimeError):
                return False
            return True
        finally:
            try:
                shutil.rmtree(temporary_root)
            except OSError:
                pass

    def configure_forticlient_save_login(self, log: Logger) -> None:
        """Persist the active local Windows user's full name in Save Login.

        FCConfig's documented export/import commands are used instead of UI
        keystroke automation or registry edits.  The temporary plain ``.conn``
        export is ACL-protected, verified after import, and removed before the
        method returns.
        """
        full_name = self._resolve_forticlient_save_login_name(log)
        if not self.is_admin_session():
            raise RuntimeError("FortiClient Save Login ayari icin uygulama yonetici olarak calismali.")
        executable = self._forticlient_config_executable()
        state_root = self._forticlient_profile_state_path().parent
        state_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix="forti-save-login-", dir=state_root))
        export_path = temporary_root / "current-vpn.conn"
        verification_path = temporary_root / "verified-vpn.conn"
        export_password = f"acik-export-{uuid.uuid4().hex}"
        cleanup_error: RuntimeError | None = None
        try:
            self._restrict_forticlient_temp_directory(temporary_root)
            export_result = self._run_quiet(
                self._forticlient_vpn_export_command(executable, export_path, export_password),
                timeout_seconds=90,
            )
            # FortiClient 7.0 can return a non-zero process code after it has
            # already written the requested export.  The readable XML and the
            # exact target tunnel are the real safety condition, so do not
            # reject a valid export solely on that code.
            if not export_path.is_file():
                raise RuntimeError(
                    "FortiClient mevcut VPN yapilandirmasi disa aktarilamadi "
                    f"(FCConfig kodu {export_result.returncode})."
                )
            if export_result.returncode != 0:
                log(
                    "FCConfig disa aktarma kodu sifir degil; yazilan VPN dosyasi "
                    "hedef tunel ve XML olarak dogrulanacak."
                )

            self._write_forticlient_save_login(export_path, full_name)
            import_result = self._run_quiet(
                self._forticlient_vpn_import_command(executable, export_path, export_password),
                timeout_seconds=90,
            )
            if import_result.returncode != 0:
                log(
                    "FCConfig ice aktarma kodu sifir degil; Save Login sonraki "
                    "disa aktarma ile dogrulanacak."
                )

            verification_result = self._run_quiet(
                self._forticlient_vpn_export_command(executable, verification_path, export_password),
                timeout_seconds=90,
            )
            saved_login_visible_in_export = (
                verification_path.is_file()
                and self._forticlient_export_has_saved_login(verification_path, full_name)
            )
            if saved_login_visible_in_export:
                if verification_result.returncode != 0:
                    log(
                        "FCConfig dogrulama kodu sifir degil; Save Login XML okuma "
                        "sonucuyla dogrulandi."
                    )
            elif import_result.returncode == 0:
                # FortiClient 7.0 persists Save Login in the interactive
                # user's configuration, while FCConfig's VPN export can show
                # only the service-side connection.  The UI therefore has the
                # correct name even though that export cannot read it back.
                # A successful official import is the authoritative outcome
                # in this client version; do not show a false failure.
                log(
                    "FortiClient Save Login kullanici oturumu ayarina uygulandi; "
                    "FCConfig disa aktarmasi bu kullanici-kapsamli alani geri gostermedi."
                )
            else:
                raise RuntimeError(
                    "FortiClient Save Login kullanici adi dogrulanamadi "
                    f"(FCConfig kodu {verification_result.returncode})."
                )
        finally:
            try:
                shutil.rmtree(temporary_root)
            except OSError:
                cleanup_error = RuntimeError("FortiClient gecici yapilandirma dosyasi kaldirilamadi.")
        if cleanup_error is not None:
            raise cleanup_error
        log("FortiClient Save Login, ACIK kullanicisinin tam adi ile guncellendi.")

    def _forticlient_vpn_profile(self) -> Path:
        """Return the encrypted, integrity-checked profile bundled with the EXE."""
        profile = self.config.base_dir / "payloads" / FORTICLIENT_VPN_PROFILE_FILE
        if not profile.is_file():
            raise RuntimeError(f"Gomulu FortiClient profili bulunamadi: payloads\\{FORTICLIENT_VPN_PROFILE_FILE}")
        self.verify_payload_integrity(profile)
        return profile

    def _forticlient_profile_state_path(self) -> Path:
        program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
        return program_data / "AcikOnboarding" / "state" / "forticlient_mkr_fc_ra.json"

    @staticmethod
    def _forticlient_profile_sha256(profile: Path) -> str:
        digest = hashlib.sha256()
        with profile.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _forticlient_profile_was_imported(self, profile: Path) -> bool:
        state_path = self._forticlient_profile_state_path()
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(state, dict)
            and state.get("connection_name") == FORTICLIENT_VPN_CONNECTION_NAME
            and state.get("profile_sha256") == self._forticlient_profile_sha256(profile)
        )

    def _mark_forticlient_profile_imported(self, profile: Path) -> None:
        atomic_write_json(
            self._forticlient_profile_state_path(),
            {
                "connection_name": FORTICLIENT_VPN_CONNECTION_NAME,
                "profile_sha256": self._forticlient_profile_sha256(profile),
                "imported_at": utc_now(),
            },
        )

    def _prepare_editable_forticlient_vpn_profile(self, profile: Path) -> None:
        """Keep the locally imported VPN editable without exposing its PSK.

        A connection marked as received from FortiGate/EMS can remain
        read-only.  Do not rewrite that marker: Fortinet documents it as
        server-managed.  Instead, reject it and require a locally exported
        connection.  For a local profile, explicitly permit personal VPN
        configuration so an administrator is not left with a locked editor.
        """
        try:
            tree = ElementTree.parse(profile)
        except (ElementTree.ParseError, OSError) as exc:
            raise RuntimeError("FortiClient profili okunamadi; .conn XML dosyasini yeniden disa aktarın.") from exc

        root = tree.getroot()
        vpn = root.find("vpn")
        if vpn is None:
            raise RuntimeError("FortiClient profili VPN ayarlarini icermiyor.")

        connections = vpn.findall("./ipsecvpn/connections/connection")
        target_connection = next(
            (
                connection
                for connection in connections
                if (connection.findtext("name") or "").strip() == FORTICLIENT_VPN_CONNECTION_NAME
            ),
            None,
        )
        if target_connection is None:
            raise RuntimeError(
                f"FortiClient profili {FORTICLIENT_VPN_CONNECTION_NAME} IPsec baglantisini icermiyor."
            )

        provisioned_marker = (target_connection.findtext("fgt") or "").strip()
        if provisioned_marker == "1":
            raise RuntimeError(
                "FortiGate/EMS tarafindan yonetilen kilitli VPN profili ice aktarilamaz; "
                "FortiClient'tan yerel, duzenlenebilir bir .conn profili disa aktarın."
            )

        options = vpn.find("options")
        if options is None:
            options = ElementTree.Element("options")
            vpn.insert(0, options)
        editable_option = options.find("allow_personal_vpns")
        if editable_option is None:
            editable_option = ElementTree.SubElement(options, "allow_personal_vpns")
        if (editable_option.text or "").strip() == "1":
            return

        editable_option.text = "1"
        temporary_profile = profile.with_name(f".{profile.name}.{uuid.uuid4().hex}.tmp")
        try:
            tree.write(temporary_profile, encoding="utf-8", xml_declaration=True)
            os.replace(temporary_profile, profile)
        except OSError as exc:
            raise RuntimeError("FortiClient profilinin duzenlenebilir ayari kaydedilemedi.") from exc
        finally:
            try:
                temporary_profile.unlink(missing_ok=True)
            except OSError:
                pass

    def import_forticlient_vpn_profile(self, log: Logger) -> bool:
        """Ensure the bundled IPsec profile is truly present in FortiClient.

        A historical marker can survive an interrupted FCConfig import.  It
        is therefore only a hint: a live VPN export must contain MKR_FC_RA
        before this method preserves the existing client configuration.
        """
        config_executable = self._forticlient_config_executable()
        profile = self._forticlient_vpn_profile()
        if self._forticlient_live_vpn_has_tunnel(log):
            log(
                f"FortiClient VPN profili canli yapilandirmada bulundu; yonetici degisikliklerini korumak icin "
                f"yeniden aktarilmadi: {FORTICLIENT_VPN_CONNECTION_NAME}"
            )
            return False
        if not self.is_admin_session():
            raise RuntimeError("FortiClient VPN profili icin uygulama yonetici olarak calismali.")
        if not FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD:
            raise RuntimeError(
                "FortiClient profil parolasi public kaynakta bulunmaz. "
                "ACIK_FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD ortam degiskenini ayarlayin."
            )
        if self._forticlient_profile_was_imported(profile):
            log(
                "FortiClient profil kaydi bulundu ancak canli yapilandirmada hedef tunel yok; "
                "profil yeniden ice aktarilarak dogrulanacak."
            )
        result = self._run(
            [
                str(config_executable),
                "-f",
                str(profile),
                "-m",
                "vpn",
                "-o",
                "import",
                "-p",
                FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD,
                "-q",
            ],
            log,
            check=False,
        )
        if not self._forticlient_live_vpn_has_tunnel(log):
            raise RuntimeError(
                "FortiClient profili ice aktarma sonrasinda canli yapilandirmada bulunamadi "
                f"(FCConfig kodu {result.returncode})."
            )
        self._mark_forticlient_profile_imported(profile)
        log(f"FortiClient IPsec profili duzenlenebilir olarak ice aktarildi: {FORTICLIENT_VPN_CONNECTION_NAME}")
        return True

    def run_usb_util_installations(
        self,
        usb_path: Path,
        selected_programs: dict[str, bool],
        log: Logger,
    ) -> list[UiMessage]:
        """Install exactly the selected USB programs and verify each result."""
        if not usb_path.exists() or not usb_path.is_dir():
            raise RuntimeError(f"USB kurulum klasoru bulunamadi: {usb_path}")
        selected = [name for name, enabled in selected_programs.items() if enabled]
        if not selected:
            raise RuntimeError("Kurulacak program secilmedi.")

        messages: list[UiMessage] = []
        installer_specs: dict[str, tuple[list[str], list[str], int]] = {
            "chrome": (["ChromeSetup.exe"], ["/silent", "/install"], 120),
            # The online installer can return -1 after it hands the actual
            # install to a child process.  Do not call that a failure while
            # FortiClient.exe/FCConfig.exe are still being created.
            "forticlient": (["FortiClientVPNOnlineInstaller.exe"], ["/quiet"], 420),
            "office": (["OfficeSetup.exe"], [], 240),
            "jre": (["jre-8u341-windows-x64.exe"], ["/s"], 180),
            "winrar": (["winrar-x64-611tr.exe"], ["/S"], 120),
        }

        for program_name in selected:
            try:
                already_installed = self.is_program_installed(program_name)
                if already_installed and program_name != "forticlient":
                    messages.append(("Bilgi", f"{program_name} zaten kurulu.", "info"))
                    log(f"Program zaten kurulu: {program_name}")
                    continue
                if program_name == "eset":
                    installer = usb_path / "PROTECT_Installer_x64_tr_TR.exe"
                    self.verify_external_payload_integrity(installer)
                    original = self.config.tools.eset_installer_path
                    self.config.tools.eset_installer_path = str(installer)
                    try:
                        self.run_eset_installer(log)
                    finally:
                        self.config.tools.eset_installer_path = original
                    verified = self._wait_for_program("eset", 180, log)
                elif program_name == "anydesk":
                    candidates = [usb_path / "AnyDesk.exe", usb_path / "AnyDesk (1).exe"]
                    installer = next((path for path in candidates if path.exists()), None)
                    if installer is None:
                        raise RuntimeError("AnyDesk yukleyicisi bulunamadi.")
                    self.verify_external_payload_integrity(installer)
                    install_dir = str(Path(self.config.tools.anydesk_install_dir).resolve())
                    self._run(
                        [str(installer), "--install", install_dir, "--silent", "--create-desktop-icon"],
                        log,
                    )
                    verified = self._wait_for_program("anydesk", 120, log)
                elif program_name == "hackbgrt":
                    candidates = [
                        usb_path / "HackBGRT-2.0.0",
                        usb_path / "hackbgrt" / "HackBGRT-2.0.0",
                    ]
                    source = next((path for path in candidates if path.exists()), None)
                    if source is None:
                        raise RuntimeError("HackBGRT klasoru bulunamadi.")
                    for required_name in ("setup.exe", "config.txt", "splash.bmp"):
                        self.verify_external_payload_integrity(source / required_name)
                    original = self.config.tools.hackbgrt_setup_path
                    self.config.tools.hackbgrt_setup_path = str(source)
                    try:
                        self.run_hackbgrt(log)
                    finally:
                        self.config.tools.hackbgrt_setup_path = original
                    verified = self._wait_for_program("hackbgrt", 30, log)
                elif program_name in installer_specs:
                    installer_result: subprocess.CompletedProcess[str] | None = None
                    if already_installed:
                        log("FortiClient zaten kurulu; gomulu VPN profilinin ilk aktarimi denetlenecek.")
                        verified = True
                    else:
                        file_names, arguments, timeout = installer_specs[program_name]
                        installer = next((usb_path / name for name in file_names if (usb_path / name).exists()), None)
                        if installer is None:
                            raise RuntimeError(f"{file_names[0]} bulunamadi.")
                        self.verify_external_payload_integrity(installer)
                        installer_result = self._run(
                            [str(installer), *arguments],
                            log,
                            check=program_name != "forticlient",
                        )
                        verified = self._wait_for_program(program_name, timeout, log)
                        if (
                            program_name == "forticlient"
                            and installer_result is not None
                            and installer_result.returncode != 0
                        ):
                            if verified:
                                log(
                                    "FortiClient yukleyicisi kod "
                                    f"{installer_result.returncode} dondurdu; dosya/registry dogrulamasi "
                                    "basarili oldugu icin kurulum kabul edildi."
                                )
                            else:
                                raise RuntimeError(
                                    "FortiClient yukleyicisi kod "
                                    f"{installer_result.returncode} dondurdu ve kurulum dogrulanamadi."
                                )
                else:
                    raise RuntimeError(f"Desteklenmeyen USB programi: {program_name}")

                if not verified:
                    raise RuntimeError("Yukleyici kapandi ancak program kurulumu dogrulanamadi.")
                if program_name == "forticlient":
                    self.import_forticlient_vpn_profile(log)
                    self._start_forticlient_profile_autoconnect(log)
                    messages.append(
                        (
                            "Basarili",
                            f"FortiClient kurulumu, {FORTICLIENT_VPN_CONNECTION_NAME} VPN profili ve otomatik baglanti baslatildi.",
                            "info",
                        )
                    )
                else:
                    messages.append(("Basarili", f"{program_name} kurulumu dogrulandi.", "info"))
            except Exception as exc:
                log(f"USB kurulumu basarisiz: {program_name} - {exc}")
                self.export_task_failure_to_usb(
                    f"USB program kurulumu: {program_name}",
                    str(exc),
                    log,
                )
                messages.append(("Hata", f"{program_name} kurulumu basarisiz: {exc}", "error"))
        return messages

    def reboot_for_format(self) -> None:
        """Bilgisayari gelismis baslangic secenekleriyle (Advanced Startup) yeniden baslatir.
        Kullanici Ventoy USB'den boot ederek format islemine baslayabilir.
        """
        import subprocess
        # shutdown /r (restart) /o (advanced startup options) /f (force) /t 0 (immediately)
        subprocess.run(["shutdown.exe", "/r", "/o", "/f", "/t", "0"], check=False)

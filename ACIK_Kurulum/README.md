# AÇIK Kurulum

*Türkçe sürüm için [README.tr.md](README.tr.md) dosyasına bakın.*

AÇIK Kurulum ("AÇIK Setup") is a Python + PySide6 onboarding application that
manages the preparation of new company Windows laptops from a single screen:
local/domain account creation, computer naming, corporate network resources,
application installs, desktop policies, reporting, and the tasks that must
run after the first reboot.

This repository is a **public source-code copy**. It is not an operational
installation package — see [Public source delivery](#public-source-delivery)
below for exactly what has been left out and why.

## What it automates

- Local or domain user account creation, with a documented, auditable
  privilege model (see [Security model](#security-model)).
- Computer renaming, Wi-Fi profile installation and time sync.
- Corporate file server and network printer connections for the new user.
- Desktop wallpaper / lock screen policy, desktop signature files, and
  Outlook Classic setup.
- Application installs: ESET, AnyDesk, Chrome, FortiClient VPN, Office, JRE,
  WinRAR, and (opt-in) HackBGRT.
- Windows activation, Windows Update, and a final scheduled restart.
- A durable, reboot-crossing workflow: steps that must run in the new user's
  own session, or as `SYSTEM`, are scheduled and tracked across restarts for
  up to 48 hours, with automatic retry and a safe timeout.
- JSON run reports plus optional webhook/Telegram notifications.

## Security model

The application never grants the operator or the new user temporary
administrator membership — that would leave the existing session token
un-elevated while over-provisioning the account. Instead, the workflow is
split into three stages:

1. **Main setup** runs in a UAC-elevated administrator session.
2. **User-specific steps** (file server, printer, wallpaper, Outlook, …) run
   only in the target user's own session, after their first sign-in.
3. **Privileged final steps** (ESET, group membership, lock screen policy,
   removal of the old provisioning account) run through a protected
   `SYSTEM` scheduled task.

The app that runs after reboot is copied to
`%ProgramData%\AcikOnboarding\app`, so the workflow does not depend on the
USB drive letter or on the USB drive staying plugged in once setup has
started.

See [`SECURITY.md`](SECURITY.md) for the full authorization model, secret
handling rules, and command/file safety notes (in Turkish, matching the rest
of the in-repo documentation).

## Running from source

```powershell
python .\run_app.py
```

`run_app.py` prepares a local `.dev-venv` environment for development,
installs missing dependencies, and requests UAC elevation on a normal run.

Tests never use real operational passwords:

```powershell
python -m pytest -q
```

> The automated test suite and the private domain-join helper are
> intentionally excluded from this public copy — see below.

## Building a clean release

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_release.ps1
```

The script installs dependencies into a separate `.build-venv`, refreshes the
payload manifest, runs the tests, and produces the result in:

```text
release\ACIK-Kurulum-v4\
```

No operational configuration or Windows product key is included in a clean
release.

To prepare an operational bundle on a BitLocker-protected USB/NTFS target:

```powershell
.\prepare_operational_bundle.ps1 -TargetDir "E:\ACIK-Kurulum-v4"
```

## Configuration

Configuration is resolved in this order:

1. `ACIK_CONFIG_PATH` environment variable
2. `app_config.local.json` next to the application
3. `private_secrets\app_config.local.json` next to the source tree
4. `app_config.example.json` (no passwords — the default in this repo)

Never keep real passwords, tokens, or product keys in source control, in a
release folder, or on a plain (unencrypted) USB drive. The operational
config file is protected by an ACL that only `Administrators` and `SYSTEM`
can read.

FortiClient VPN support is source-only here. A private build must supply its
own approved `.sconf` profile (see `payloads/README.md`) and provide its
export password at run time through the
`ACIK_FORTICLIENT_VPN_PROFILE_EXPORT_PASSWORD` environment variable. No
FortiClient connection details are included in this public copy.

## Project layout

- `run_app.py` — UAC elevation, single-instance lock, and run-mode selection
  (main UI / post-login / SYSTEM finalize / recovery).
- `src/acik_onboarding/app.py` — wires the three run modes to the service and
  UI layers.
- `src/acik_onboarding/ui.py` — the PySide6 interface and its background
  workers.
- `src/acik_onboarding/services.py` — Windows operations and the onboarding
  business logic (by far the largest module: PowerShell generation, account
  management, scheduled tasks, reporting).
- `src/acik_onboarding/workflow.py` — the durable task/phase state model that
  survives reboots.
- `src/acik_onboarding/config.py` — the typed configuration model and its
  safe JSON (de)serialization.
- `tests/` — identity, command, and post-reboot workflow tests (not included
  in this public copy; see below).
- `TROUBLESHOOTING.md` — field diagnostics and common failure modes.
- `SECURITY.md` — authorization and secret-handling rules.

Windows account behavior, domain join, profile deletion, printer drivers, and
UEFI changes all depend on the real device. Before cutting a release, test
end-to-end on a clean Windows VM configured with the same GPOs/network as
production, and on at least one pilot laptop.

## Public source delivery

This copy of the source (version `5.21.31`) intentionally **excludes**:

- `app_config.local.json` and `private_secrets/`
- Domain, Wi-Fi, local-admin, backup, product-key, webhook, API-token, and
  VPN credentials
- Encrypted VPN profiles, plain VPN exports, certificates, and private keys
- Third-party installer executables, boot assets, release output, logs, and
  diagnostic records
- The private domain-join helper and the automated test fixtures

`app_config.example.json` is the password-free example configuration. For a
private deployment, keep the real configuration in a protected location and
select it with `ACIK_CONFIG_PATH`; do not add it to this repository.

Before publishing a new snapshot of this source, regenerate the delivery
file manifest and re-run the same secret scan used to verify this delivery.

## Requirements

- Windows 10/11, Python 3.11+, PySide6 (`requirements.txt`)
- `pytest` for the test suite (`requirements-dev.txt`)
- PyInstaller for release builds (`requirements-build.txt`)

## License

No license file is currently included in this repository. Until one is
added, treat this source as "all rights reserved" — check with the
maintainers before reusing it outside this project.

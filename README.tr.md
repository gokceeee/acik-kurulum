# AÇIK Kurulum

AÇIK Kurulum, yeni Windows cihazların hazırlanmasını tek ekranda yöneten
Python ve PySide6 tabanlı bir onboarding uygulamasıdır. Yerel/domain hesap
akışı, bilgisayar adı, kurumsal ağ kaynakları, uygulama yüklemeleri, masaüstü
ilkeleri, raporlama ve yeniden başlatma sonrası görevleri birlikte yönetir.

## Güvenli Çalışma Modeli

Uygulama kullanıcıya geçici yönetici üyeliği vermez. Bu yöntem mevcut oturum
belirtecini yükseltmediği için güvenilir değildir ve kullanıcıya gereğinden
fazla yetki açar. Akış bunun yerine üçe ayrılır:

1. Ana kurulum, UAC ile yükseltilmiş yönetici oturumunda çalışır.
2. File Server, yazıcı, duvar kağıdı ve Outlook gibi kullanıcıya özgü adımlar
   yalnızca hedef kullanıcı oturumunda çalışır.
3. ESET, grup üyelikleri, kilit ekranı ve eski `x` hesabının silinmesi gibi
   ayrıcalıklı son adımlar korumalı bir `SYSTEM` göreviyle tamamlanır.

Yeniden başlatma sonrası çalışacak uygulama `%ProgramData%\AcikOnboarding\app`
altına kopyalanır. Bu nedenle kurulum başladıktan sonra USB sürücü harfine veya
USB'nin takılı kalmasına bağımlı değildir.

## Kaynaktan Çalıştırma

```powershell
python .\run_app.py
```

`run_app.py` geliştirme için `.dev-venv` ortamını hazırlar, eksik bağımlılıkları
kurar ve normal akışta UAC yükseltmesi ister.

Testler gerçek operasyon parolalarını kullanmaz:

```powershell
python -m pytest -q
```

## Temiz Sürüm Üretme

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_release.ps1
```

Betik bağımlılıkları ayrı `.build-venv` ortamına kurar, payload manifestini
yeniler, testleri çalıştırır ve sonucu şu klasöre üretir:

```text
release\ACIK-Kurulum-v4\
```

Operasyon ayarı veya Windows ürün anahtarı temiz release içine girmez.

BitLocker korumalı bir USB/NTFS hedefe operasyon paketi hazırlamak için:

```powershell
.\prepare_operational_bundle.ps1 -TargetDir "E:\ACIK-Kurulum-v4"
```

## Yapılandırma

Yapılandırma önceliği:

1. `ACIK_CONFIG_PATH`
2. Uygulama yanındaki `app_config.local.json`
3. Kaynak ağacının yanındaki `private_secrets\app_config.local.json`
4. Parolasız `app_config.example.json`

Gerçek parola, token ve ürün anahtarlarını kaynak kodda, release klasöründe
veya normal şifrelenmemiş USB'de tutmayın. Operasyon dosyası yalnızca
Administrators ve `SYSTEM` tarafından okunacak ACL ile korunur.

## Proje Haritası

- `run_app.py`: UAC, tek örnek kilidi ve çalışma modu seçimi.
- `src/acik_onboarding/ui.py`: PySide6 arayüzü ve arka plan işçileri.
- `src/acik_onboarding/services.py`: Windows işlemleri ve onboarding akışı.
- `src/acik_onboarding/workflow.py`: Kalıcı görev/faz durum modeli.
- `src/acik_onboarding/config.py`: Güçlü tipli ayar modeli ve güvenli kayıt.
- `tests/`: Kimlik, komut ve yeniden başlatma sonrası akış testleri.
- `DEVELOPER_GUIDE.md`: Mimari ve yeni özellik ekleme rehberi.
- `TROUBLESHOOTING.md`: Saha hataları ve teşhis adımları.
- `SECURITY.md`: Yetki ve sır yönetimi kuralları.

Windows hesabı, domain katılımı, profil silme, yazıcı sürücüsü ve UEFI
değişiklikleri gerçek cihaz davranışına bağlıdır. Release alınmadan önce
kurumla aynı GPO ve ağ koşullarına sahip temiz bir Windows sanal makinesi ve
en az bir pilot laptop üzerinde uçtan uca test yapılmalıdır.

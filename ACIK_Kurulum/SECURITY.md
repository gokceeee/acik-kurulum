# Güvenlik Notları

## Yetki Modeli

Ana UI yönetici olarak çalışır. Kullanıcıya geçici admin üyeliği verilmez.
Yönetici gerektiren yeniden başlatma sonrası adımlar Windows Scheduled Task
üzerinden `SYSTEM` olarak çalışır. Kullanıcı fazı yalnızca hedef kullanıcı
bağlamında gerekli masaüstü/ağ görevlerini yürütür.

Korumalı state:

- `%ProgramData%\AcikOnboarding\runtime\system`: SYSTEM ve Administrators tam.
- `%ProgramData%\AcikOnboarding\app`: SYSTEM/Administrators tam, Users yalnızca
  oku ve çalıştır.
- Kullanıcı planı ayrı klasördedir ve ayrıcalıklı görev alanlarını içermez.
- Kullanıcı progress verisi yalnızca allowlist kullanıcı görevlerine birleştirilir.

## Sırlar

Gerçek sırlar yalnızca `private_secrets\app_config.local.json` veya operasyon
USB'sindeki korumalı `app_config.local.json` içinde tutulur. Kaynak, test ve
temiz release sadece boş `app_config.example.json` kullanır.

Korunması gereken alanlar:

- Domain hesabı ve parolası.
- Wi-Fi parolası.
- Lokaladm parolası.
- Şirket kullanıcı parolaları.
- Backup/File Server kimlik bilgileri.
- Webhook/Telegram tokenları.
- Windows ürün anahtarı.

Operasyon paketi BitLocker korumalı NTFS hedefe hazırlanmalıdır. Düz FAT/exFAT
USB, Windows ACL uygulayamadığı için sır taşımaya uygun değildir.

Geçmişte kaynak veya eski dağıtım klasöründe bulunan gerçek parolalar artık
silinmiş olsa bile açığa çıkmış kabul edilmeli ve ilgili sistemlerde
değiştirilmelidir.

## Ayarlar Parolası

Ayarlar ekranı UAC ile yükseltilmiş oturum ve uygulama içi parola ister. Beş
hatalı girişten sonra 60 saniye kilitlenir. Bu parola yalnızca yanlışlıkla ayar
değiştirmeyi zorlaştıran ikinci bir UI korumasıdır; dosya ACL'si ve Windows
yetkilendirmesinin yerini tutmaz.

Parolayı değiştirmek için yeni SHA-256 değerini üretip `ui.py` içindeki
`SETTINGS_PASSWORD_HASH` sabitini güncelleyin. Parolanın düz metnini kaynak
veya dokümana yazmayın.

## Payload Bütünlüğü

`payload_catalog.py`, payload dosyalarının beklenen boyut ve SHA-256 değerlerini
EXE içine gömer. `payload_manifest.json` kullanıcıya açık yardımcı manifesttir;
güven kararının tek kaynağı değildir.

Payload değiştiğinde:

```powershell
.\tools\generate_payload_manifest.ps1
python -m pytest -q
.\build_release.ps1
```

İmzası/özet değeri doğrulanamayan installer çalıştırılmaz.

## Komut ve Dosya Güvenliği

- PowerShell girdileri tek tırnak kaçışıyla işlenir.
- Parola komut satırı argümanına veya loga yazılmaz.
- Ağ parolası gecikmeli state içinde DPAPI LocalMachine ile korunur.
- Yerel hesap çakışmasında yalnızca uygulamanın daha önce oluşturduğu ve
  marker'ı eşleşen hesap güncellenir.
- Profil silme `C:\Users` kökü, özel profil ve reparse point kontrolleriyle
  fail-closed çalışır.
- Webhook yalnızca HTTPS adresini kabul eder.
- Workflow ve user plan 48 saat sonra geçersiz olur.

## Release Güveni

`build_release.ps1` test, PyInstaller build ve release manifesti üretir.
Üretim EXE'si ayrıca kurumun Authenticode sertifikasıyla imzalanmalıdır.
`ACIK_SIGN_CERT_SHA1` tanımlıysa build betiği `signtool` ile imzalar ve
doğrular. Sertifika yoksa çıkan EXE işlevsel fakat imzasızdır.

Release öncesi:

1. Gerçek config veya ürün anahtarının release içinde olmadığını doğrulayın.
2. `release_manifest.json` dosyasını arşivleyin.
3. SmartScreen ve Defender davranışını pilot cihazda test edin.
4. Domain, yazıcı ve güvenlik ürünü GPO'larıyla uçtan uca test yapın.
5. Dağıtımda kullanılan tüm operasyon parolalarının güncel olduğunu doğrulayın.

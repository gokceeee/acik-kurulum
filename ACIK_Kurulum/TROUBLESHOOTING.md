# Sorun Giderme

## Uygulama Açılmıyor

1. Görev Yöneticisi'nde başka `ACIK-Kurulum.exe` örneği olmadığını kontrol edin.
2. UAC isteğinin iptal edilmediğini doğrulayın.
3. `app_config.local.json` ACL'sinde Administrators ve `SYSTEM` bulunduğunu
   kontrol edin.
4. Kaynaktan çalışıyorsa bozuk `.dev-venv` klasörünü kaldırıp tekrar başlatın.
5. EXE yanındaki `app_config.example.json` ve `assets` içeriğini kontrol edin.

Başlangıç hataları Windows mesaj kutusunda gösterilir. Standart kullanıcı özel
config'i okuyamıyorsa bu beklenen bir korumadır; normal ana UI yönetici olarak
açılmalıdır.

## Ön Kontrol Sürekli Geçersiz

Ön kontrolden sonra ad, şirket, profil, kullanıcı tipi, PC adı veya herhangi
bir kurulum seçeneği değişirse fingerprint değişir. `Sistemi Kontrol Et`
düğmesini yeniden çalıştırın.

## İkinci Faz Açılmıyor

Kontrol edilecek yollar:

```text
%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup\AcikPostLogin.cmd
%ProgramData%\AcikOnboarding\app\
%ProgramData%\AcikOnboarding\runtime\user\<run_id>\
%LOCALAPPDATA%\AcikOnboarding\logs\
```

- Hedef kullanıcı adı startup argümanıyla aynı olmalıdır.
- Başka bir kullanıcı oturum açtıysa helper sessizce çıkar ve hedef kullanıcıyı
  bekler.
- Domain hesabı ilk yeniden başlatmadan sonra SID üretmiş olmalıdır.
- Yerel kopyanın ACL'sinde Users için `RX`, Administrators/SYSTEM için `F`
  bulunmalıdır.

## SYSTEM Finalizer Tamamlanmıyor

Görev Zamanlayıcı'da `AcikOnboardingFinalize-<run_id>` görevini kontrol edin.
Log:

```text
%ProgramData%\AcikOnboarding\runtime\system\system_finalize.log
```

Finalizer kullanıcı sonucu için en fazla 30 dakika bekler. Sonuç yoksa görev
bir sonraki oturum açılışında yeniden çalışır. Workflow 48 saat dolarsa açık
adımlar hata olur ve güvenli temizlik yapılır.

## Wi-Fi veya Saat Eşitleme

- Beklenen SSID ayarlardaki `required_wifi_ssid` değeridir.
- Profil bilgisayara eklenmiş olmalıdır.
- `netsh wlan show interfaces` çıktısında bağlı SSID'yi kontrol edin.
- Kablosuz adaptörün adı sabit kabul edilmez; SSID üzerinden sorgulanır.
- Saat için Windows Time servisinin ve kurum NTP/GPO erişiminin açık olduğunu
  kontrol edin.

## Domain Katılımı

- DNS, domain denetleyicisini çözmelidir.
- Domain kullanıcı/parolasını ve domain adını özel config'te doğrulayın.
- Aynı bilgisayar adı AD'de varsa yeniden kullanım politikasını kontrol edin.
- Katılım sonrası yeniden başlatma zorunludur.
- Domain hesabının ilk girişinden önce kullanıcı fazı başlamaz.

## File Server

- `\\host\share` yolunu hedef kullanıcı oturumunda elle test edin.
- Kullanıcı adı `ACIK\<üretilen_kullanıcı>` biçiminde hazırlanır.
- Parola state içinde DPAPI LocalMachine ile korunur ve rapora/loga yazılmaz.
- Aynı sunucuya farklı kimlikle açık SMB oturumu varsa Windows 1219 hatası
  verebilir; mevcut bağlantıları güvenli biçimde kapatıp yeniden deneyin.
- Kısayol hedef kullanıcının masaüstünde oluşturulur.

## Ağ Yazıcısı

- `\\10.9.10.250\acik_printer` erişimini hedef kullanıcıda test edin.
- Bağlantı WNet, WScript.Network ve `Add-Printer` ile denenir.
- Driver yüklemesi yönetici istiyorsa geçici admin vermeyin. Kurum GPO'sunda
  onaylı print server/Point and Print ayarı yapın veya sürücüyü imaja önceden
  ekleyin.

## ESET veya AnyDesk

- Payload dosyasının adı, boyutu ve SHA-256 değeri gömülü katalogla eşleşmelidir.
- Payload değiştiyse `tools\generate_payload_manifest.ps1` çalıştırıp yeniden
  release alın.
- ESET geçici ve benzersiz bir klasörden başlatılır; kurulum sonrası servis/
  registry postcondition'ı beklenir.
- AnyDesk yükleyicisi yoksa HTTPS indirme denenir; internet/proxy erişimini
  kontrol edin.

## HackBGRT

- Firmware UEFI olmalıdır.
- Secure Boot kapalı olmalıdır.
- `setup.exe` ve EFI dosyaları payload manifestiyle eşleşmelidir.
- Kurulum sonucu `setup.log` içindeki install/enable-entry işaretleriyle
  doğrulanır.
- Firmware değişikliklerini yalnızca pilot cihazda test edin; kurtarma medyası
  hazır bulundurun.

## `x` Kullanıcısı Silinmiyor

Bu güvenli davranıştır. Silme yalnızca diğer tüm açık görevler başarılı veya
bilinçli atlanmışsa çalışır.

- `x` oturumu kapatılamadıysa görev durur.
- Hedef kullanıcı, lokaladm, mevcut kullanıcı veya Administrator silinemez.
- Profil kökü `C:\Users` dışında ise silinmez.
- Profil kökünde ya da altında reparse point varsa silinmez.
- Önce rapordaki kalıcı/yeniden denenebilir hatayı düzeltin.

## Log ve Raporlar

```text
%ProgramData%\AcikOnboarding\reports\
%ProgramData%\AcikOnboarding\runtime\system\
%LOCALAPPDATA%\AcikOnboarding\logs\
```

Raporlarda parola/token bulunmamalıdır. Destek için paylaşmadan önce kullanıcı
adı, seri numarası ve cihaz adını kurum politikasına göre maskeleyin.

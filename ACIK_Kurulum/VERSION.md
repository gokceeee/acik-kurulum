# AÇIK Kurulum v5.21.31

## v5.21.31 X SYSTEM görevi başlamazsa otomatik handoff kurtarması

- V5.7 korumalı SYSTEM X silme zinciri ana yol olarak korunur.
- SYSTEM X görevi 150 saniye içinde hiç başlamazsa watchdog, eski planı kapatmadan hedef kullanıcıya geçiş için SYSTEM yeniden başlatması yapar.
- Watchdog yalnızca X görevinin hiç başlamadığı durumda çalışır; X silmez. Hedef oturumdaki SYSTEM finalizer hesap, profil ve ProfileList doğrulamasını tamamlar.
- Yerel kullanıcı handoff'unda Windows yerel kullanıcı seçimi SYSTEM bağlamında doğrulanır.

# AÇIK Kurulum v5.21.30

## v5.21.30 askıda kalan X temizleme handoff kurtarması

- Awaiting post-login durumunda X temizleme görevi başlayamazsa, `Sürece Devam Et` artık eski planı kapatmadan hedef kullanıcıya geçiş için SYSTEM yeniden başlatması planlar.
- Hedef oturum sonrasında mevcut SYSTEM finalizer, X hesabını, profilini ve ProfileList kaydını doğrulayarak temizlemeye devam eder.
- Windows etkinleştirme başarısızlığı kritik olmayan bir adım olarak bu handoff akışını engellemez.

# AÇIK Kurulum v5.21.29

## v5.21.29 X temizleme akışını koruyan isteğe bağlı HackBGRT atlama

- HackBGRT seçili olsa bile cihaz UEFI/Secure Boot ön koşullarını karşılamıyorsa seçenek uyarıyla kapatılır.
- HackBGRT isteğe bağlı olduğundan, kullanıcı oluşturma, rapor ve SYSTEM doğrulamalı X silme zincirini artık durdurmaz.
- X kullanıcısının silinmesi, profil klasörü ve ProfileList doğrulamasını yapan korumalı SYSTEM akışı değiştirilmedi.

# AÇIK Kurulum v5.21.28

## v5.21.28 yeniden başlatma güvenilirliği

- İlk kullanıcı geçişi ve bekleyen akıştaki manuel yeniden başlatma, doğrudan
  `shutdown.exe` çağrısı yerine SYSTEM hesabında çalışan görev ile yapılır.
- Görev, eski yarım kalmış yeniden başlatma isteğini temizler; `shutdown.exe`
  kod döndürürse SYSTEM `Restart-Computer -Force` yedeğini dener.
- Görev Zamanlayıcı kullanılamazsa, yükseltilmiş `Restart-Computer` yedeği
  uygulanır ve hata gizlenmez.
- X temizleme zinciri değiştirilmedi.

# AÇIK Kurulum v5.21.27

## v5.21.27 domain katılım kimliği ayrımı

- Bilgisayar domain'e, operasyon yapılandırmasındaki yetkili domain kimliğiyle
  alınır. Hedef/oluşturulacak kullanıcının parolası domain katılım komutunu
  artık doğrulamaz veya engellemez.
- Hedef kullanıcının oturum parolası, yalnızca kendi Windows oturum akışı için
  korunmaya devam eder; tanılama kayıtlarına yazılmaz.
- X temizleme, yerel kullanıcı, FortiClient ve yeniden başlatma akışları
  değiştirilmedi.

# AÇIK Kurulum v5.21.26

## v5.21.26 FortiClient IPsec profil anahtari yenilemesi

- FortiClient'in yerel baglanti duzenleyicisiyle guncellenen IPsec anahtari,
  Fortinet'in destekledigi FCConfig VPN disa aktarma/ice aktarma akisi ile
  yeni paket profiline alindi.
- Paket profili kullaniciya ait Save Login verisini tasimaz: kurulumdan sonra
  Forti Giris, aktif yerel Windows kullanicisinin Tam ad bilgisini yeniden
  uygular. VPN sunucusu, tünel adi ve diger IPsec ayarlari korunur.
- X temizleme, kullanici, domain ve yeniden baslatma akislari degistirilmedi.

## v5.21.25 FortiClient kullanici kapsami yanlis hata duzeltmesi

- FortiClient 7.0, Save Login'i etkileşimli kullanici ayarinda tutarken
  FCConfig VPN disa aktarmasi servis-kapsamli baglanti degerlerini gosterebilir.
  Bu nedenle GUI'de dogru gorunen ad/ayarlar uygulanmis olsa bile eski surum
  yanlis bir "kullanici adi dogrulanamadi" hatasi veriyordu.
- Resmi FCConfig ice aktarmasi basariliysa bu kullanici-kapsamli sonuc basari
  kabul edilir; ayrica disariya gorunuyorsa XML ile dogrudan dogrulanir.
- X temizleme, kullanici, domain ve yeniden baslatma akislari degistirilmedi.

# AÇIK Kurulum v5.21.24

## v5.21.24 FortiClient Save Login XAuth isteme bayragi

- Canli `MKR_FC_RA` tünelinde `xauth/prompt_username=1` oldugu
  dogrulandi. FortiClient 7.0 bu durumda kayitli kullanici adini saklamaz.
  Save Login artik `ui/save_username=1`, yerel Windows Tam ad ve
  `xauth/prompt_username=0` degerlerini birlikte yazar ve ucluyu geri okur.
- Diger VPN ayarlari, parola ve endpoint degerleri degistirilmez.
- X temizleme, kullanici, domain ve yeniden baslatma akislari degistirilmedi.

# AÇIK Kurulum v5.21.23

## v5.21.23 FortiClient canli tünel ve profil dogrulamasi

- FCConfig 7.0.14'te dosya uretmeyen `exportvpn` yerine, bu cihazda dosya
  ve XML urettigi dogrulanan resmi `-m vpn -o export -p ... -q` akisi
  kullanilir.
- Eski ProgramData "profil ice aktarildi" kaydi tek basina yeterli degildir.
  Forti Giris, canli XML'de `MKR_FC_RA` tünelini arar; kayit olsa bile tünel
  yoksa resmi profili yeniden ice aktarir ve tünel gorunmeden basarili saymaz.
- Save Login sadece bu dogrulamadan sonra hedef tüneldeki iki ilgili alani
  gunceller; diger VPN alanlari korunur.
- X temizleme, kullanici, domain ve yeniden baslatma akislari degistirilmedi.

# AÇIK Kurulum v5.21.22

## v5.21.22 FortiClient FCConfig sonuc-dogrulamasi

- Bazi FortiClient 7.0 kurulumlari `exportvpn` veya `importvpn` isleminden
  sonra dosyayi basariyla yazsa da sifir olmayan bir islem kodu dondurur.
  Forti Giris artik sadece bu koda bakmaz; uretilen XML'deki `MKR_FC_RA`
  tünelini ve Save Login son-okumasini dogrular.
- Gercekten hic disa aktarma dosyasi uretilmezse hata iletisi FCConfig kodunu
  bildirir; VPN ayari degistirilmeden islem durur.
- X temizleme, kullanici, domain ve yeniden baslatma akislari degistirilmedi.

# AÇIK Kurulum v5.21.21

## v5.21.21 FortiClient FCConfig 7.0 export uyumlulugu

- Forti Giris Save Login adimindaki FCConfig disa aktar/ice aktar komutlarindan
  desteklenmeyen `-i 1` parametresi kaldirildi. Kurulu FortiClient 7.0.14
  surumunun resmi soz dizimi olan `-m vpn -f ... -o exportvpn|importvpn -q`
  kullanilir.
- Bu nedenle gecerli `MKR_FC_RA` baglantisinda gorulen "mevcut VPN
  yapilandirmasi disa aktarilamadi" hatasi ortadan kalkar.
- X temizleme, kullanici, domain ve yeniden baslatma akislari degistirilmedi.

# AÇIK Kurulum v5.21.20

## v5.21.20 FortiClient Save Login yerel Windows tam adi

- Forti Giris artik kurulum formundaki Ad Soyad degerini ve eski raporlari
  kullanmaz. Yalnizca aktif yerel Windows hesabinin Bilgisayar Yonetimi >
  Yerel Kullanicilar ve Gruplar > Kullanicilar ekranindaki Tam ad
  (`Get-LocalUser.FullName`) degerini `MKR_FC_RA` tünelinin Save Login alanina
  yazar.
- Domain oturumunda veya yerel hesabin Tam ad alani bos/okunamaz durumdaysa
  yanlis bir hesabi kaydetmek yerine islem acik bir hata ile durur.
- X temizleme, kullanici olusturma, domain ve yeniden baslatma akislari
  degistirilmedi.

# AÇIK Kurulum v5.21.19

## v5.21.19 live log layout and restart recovery

- Canli islem gunlugu filtreleri dar bir denetim alaniyla ikili veya uclu
  satirlara yayilir; kirmizi Temizle dugmesi Hata filtresinin hemen yanindadir.
- Bekleyen ikinci faz plani icin ACL onarimi, `icacls /T` alt oge sonucundan
  bagimsiz olarak plan dosyasinin gercekten okunabildigini dogrular. Bu nedenle
  ilgisiz bir alt oge hatasi yeniden baslatma/devam akisini durdurmaz.
- Forti Giris, resmi FCConfig disa aktar/ice aktar akisi ile yalnizca
  `MKR_FC_RA` tünelinin Save Login kullanici adini kurulumdaki ACIK
  kullanicisinin tam adi olarak kaydeder; parola ve diger VPN ayarlari
  degistirilmez.
- X temizleme, kullanici olusturma ve domain akislarina dokunulmadi.

# AÇIK Kurulum v5.21.18

## v5.21.18 local standard-user fixed desktop wallpaper

- Sabit masaustu arka plani sadece kurulumun olusturdugu yerel standart
  kullaniciya uygulanir; domain ve yerel yonetici hesaplar bu ilkenin disinda
  kalir.
- SYSTEM finalizer, profil hive'i anlik yuklu degilse hedef kullanicinin
  NTUSER.DAT dosyasini gecici olarak yukler, resmi Desktop Wallpaper ve
  Prevent changing desktop background ilke degerlerini dogrular ve tekrar
  kaldirir.
- ACL sadece ilgili iki duvar kagidi ilke anahtarina daraltildi; profilin tum
  Policies dali artik degistirilmiyor.

## v5.21.17 FortiClient 7.0 compatibility and installer verification

- `Bağlan`, `FortiVPN.exe` olmayan FortiClient 7.0.x sistemlerinde içe aktarılan
  otomatik bağlantı profilini kullanarak FortiClient'ı arka planda başlatır.
- VPN profili, kurulumdan sonra ve FortiClient başlatıldığında `MKR_FC_RA`
  tüneline otomatik bağlanmayı ister.
- Çevrimiçi FortiClient yükleyicisinin alt kurulum sürecine devretmesinden sonra
  döndürebildiği kod 1/-1 için program dosyası doğrulama süresi 7 dakikaya çıkarıldı.

## v5.21.16

## v5.21.16 FortiClient CLI connect and compact workflow actions

- `Bağlan` now invokes Fortinet's documented Windows `FortiVPN.exe` CLI for
  the imported `MKR_FC_RA` tunnel, then verifies its connected status without
  placing a password on a process command line.
- The Forti profile exposes FortiClient's Auto Connect option for new
  installations, while the button itself directly requests a connection.
- The three onboarding actions now share one compact row.

## v5.21.15 live-log controls

## v5.21.15 live-log controls

- The red `Temizle` control now sits beside the Canlı İşlem Günlüğü title,
  keeping log-only work with the log itself.
- The main onboarding action grid now contains only its three workflow
  controls; clearing the live log never changes reports or onboarding state.

## v5.21.14 manual FortiClient connection

- Post-restart automatic FortiClient launch was removed from the onboarding
  workflow.
- USB Araçları now provides a `Bağlan` button immediately left of `Kur` on the
  FortiClient row. It becomes available only after the installed client is
  detected and opens that existing client without reinstalling it.
- A Forti online-installer exit code of 1 is accepted only when the installed
  program is subsequently verified by file or registry detection.

## v5.21.12 workflow controls

- The four live workflow actions now remain in a two-by-two grid at all window
  sizes: generate/start on the first row, terminate/clear log on the second.
- Log clearing moved from the live-log title to a red action button. It clears
  only the visible live log; reports and installation state are untouched.

## v5.21.11 domain sign-in clarity

- The form now shows the exact short-domain Windows sign-in identifier for a
  Domain installation (for example, `ACIK\\username`) and repeats it in the
  completion notice. The password is never displayed or recorded.
- Target credential validation from v5.21.10 remains before the domain join;
  this release only makes the required Windows logon identity explicit.

## v5.21.10 domain login and recovery placement

- Domain onboarding now validates the selected target domain username and its
  supplied password before the computer is joined. Invalid target credentials
  stop the run before the device membership is changed.
- The old-domain recovery action is now a direct, administrator-only button
  inside the "Kurulum ve Sistem" section. It no longer appears in the main
  start card and does not require starting an onboarding run.

## v5.21.9 X cleanup diagnostics

## v5.21.9 X cleanup diagnostics

- The SYSTEM X-cleanup task now records to the USB audit log even if a damaged
  protected ProgramData ACL prevents writing its local audit file. This keeps
  a failed cleanup diagnosable without changing the verified cleanup or final
  restart sequence.

## v5.21.8 protected workflow recovery

- Elevated recovery now retries the protected post-login plan ACL after
  securely taking ownership when an interrupted ACL operation blocked the
  Administrators group. The retry restores access only for SYSTEM and
  Administrators; it does not expose the plan to the target user.
- This changes only the manual recovery path. The verified X-cleanup
  sequence remains unchanged.

## v5.21.7 domain recovery

- Added an explicit, administrator-only "Domainden Çık" recovery action.
  It asks for the approved domain username, uses the protected configured
  password without displaying or logging it, requests a WORKGROUP unjoin,
  restores the local account picker, and schedules a 15-second reboot.
- The action skips safely when the device is already outside a domain and
  never cancels or changes an existing onboarding/X-cleanup workflow.

# AÇIK Kurulum v5.21.6

## v5.21.6 source audit

- Domain second-phase continuation and SYSTEM finalization now require the
  complete DOMAIN\\user identity. A same-named local account cannot start a
  domain workflow or trigger its privileged finalization.
- Fixed desktop wallpaper remains limited to the installer-created local,
  non-administrator account. Domain and administrator states are rejected a
  second time by the SYSTEM policy step.
- The SYSTEM phase waits for the user wallpaper phase before applying the
  policy lock or entering the verified X-cleanup path.
- Invalid, unused recovered Python artifacts were removed from the import
  tree and retained locally only in a quarantine folder for recovery.

# AÇIK Kurulum v5.7

- Paket tarihi: 27 Temmuz 2026
- Uygulama: Windows cihaz kurulum ve onboarding otomasyonu
- Kaynak dili: Python 3.12 / PySide6

## v5.7 düzeltmesi

- X temizliği artık yalnızca hesap, profil ve kayıt kalıntıları doğrulandıktan
  sonra tamamlandı görünür.
- Güvenli (LSA) AutoLogon saklaması da silinir ve doğrulanır; böylece yeniden
  başlatma sonrası X oturumu otomatik açılamaz.
- X temizliği artık eski, çalıştığı doğrulanmış davranışla aynı aşamada başlar:
  raporlar yazıldıktan sonra SYSTEM görevi X oturumunu kapatır, hesabı ve
  profilini doğrular, sonra yeniden başlatır.

## Geliştirici Başlangıcı

1. Windows üzerinde `python run_app.py` komutunu çalıştırın.
2. İlk açılışta `.dev-venv` otomatik oluşturulur ve `requirements.txt` yüklenir.
3. Testler için `python -m pytest -q` komutunu kullanın.
4. EXE üretmek için PowerShell'de `./build_release.ps1` çalıştırın.

## Paket Politikası

Bu kaynak paket; uygulama kodunu, testleri, dokümanları, görsel varlıkları ve
operasyon payload dosyalarını içerir. Sanal ortamlar, eski derlemeler, çalışma
raporları, önbellekler ve `app_config.local.json` gibi cihaz/şirket sırları
bilerek dahil edilmez. Gerçek domain, Wi-Fi, yerel yönetici ve API bilgileri
yalnızca yetkili cihazdaki yerel ayarlar ekranından girilmelidir.
# ACIK Kurulum v5.14

## v5.14 fix

- X silme zinciri V5.7 referansina kilitlendi: AutoLogon/LSA temizligi, SYSTEM
  silme gorevinin kaydindan once gerceklesir; hesap ve profil dogrulanmadan
  yeniden baslatma yapilmaz.
- X silindikten sonra, Windows'un resmi `EnumerateLocalUsers` ilkesi ile yerel
  hesap secicisi dogrulanir. Ilke uygulanamazsa domain kimlik ekraniyla
  yeniden baslatma yapilmaz.
- Yukseltilmis bir oturum korumali durum dosyasini okuyamazsa ACL onarilir ve
  kurtarma karti yenilenir; zaten yonetici olan ekranda ikinci UAC penceresi
  acilmaz.

# ACIK Kurulum v5.13

## v5.13 fix

- Korumali post-login plani standart hedef kullanicida goruldugunde, islevsiz
  "Yeniden Baslat" ve "Surece Devam Et" dugmeleri artik gosterilmez.
- Kart, UAC ile ayri bir yonetici kurtarma ekranini acan etkin bir dugme
  sunar. Bu ekran bekleyen plani korur ve gercek devam/yeniden baslatma
  islemlerini yetkili oturumda calistirir.
- Kurtarma, kapatilmis post-login paketinden acilsa bile X temizligi icin
  secilen eski hesap ve yerel yonetici adlarini korumali durum kaydindan alir.

# ACIK Kurulum v5.12

## v5.12 fix

- X temizligi, kanitlanmis SYSTEM siralamasina geri dondu: X oturumu kapatilir, kalan X surecleri zorla sonlandirilir, hesap ve C:\\Users profili dogrulanarak silinir, sonra yeniden baslatilir.
- X silinmesinden sonra domain cihazlarda yerel hesaplari gosteren resmi Windows Logon ilkesi etkinlestirilir; yerel kullanici secimi domain kimlik bilgisi ekranina donmez.

# ACIK Kurulum v5.11

## v5.11 fix

- Windows 11 Pro icin kilit ekrani, desteklenen SYSTEM Personalization CSP ile uygulanir; uygulama, CSP'nin dosyayi kabul ettigini durum kodu ile dogrular.
- Kilit ekrani gorseli USB veya korumali uygulama klasoru yerine pre-logon okunabilir yerel ProgramData klasorune kopyalanir ve LockApp erisimi icin ACL ile korunur.

# ACIK Kurulum v5.10

## v5.10 fix

- X AutoLogon ve LSA saklamasi yerel yonetici oturumunda degil, X silme
  zincirinin dogrulanmis SYSTEM gorevinde temizlenir.
- `Policies\\System` izin reddi artik ana akisi kesmez; SYSTEM temizligi
  basarisiz olursa X silinmez ve yeniden baslatma yapilmaz.
- Yerel kullanici secicisi SYSTEM adiminda normal duruma
  (`DontDisplayLastUserName=0`) getirilir.

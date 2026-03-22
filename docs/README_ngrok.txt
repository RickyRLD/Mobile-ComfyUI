JAK USTAWIC STALY ADRES NGROK (darmowy, bez domeny)
=====================================================

Czas: ~3 minuty. Darmowe na zawsze. Stały adres HTTPS.
Działa poza domem (LTE/5G) i wspiera push notifications.


KROK 1: Zaloz darmowe konto ngrok
-----------------------------------
Wejdz na: https://dashboard.ngrok.com/signup
Email + haslo.


KROK 2: Pobierz ngrok.exe
--------------------------
Wejdz na: https://ngrok.com/download
Pobierz wersje Windows, rozpakuj
Wrzuc ngrok.exe do: C:\AI\ngrok.exe


KROK 3: Skopiuj swoj authtoken
--------------------------------
Po zalogowaniu wejdz na:
https://dashboard.ngrok.com/get-started/your-authtoken

Skopiuj token (dlugi ciag znakow)


KROK 4: Wklej token do menedzer_tray.py
-----------------------------------------
Otworz: C:\AI\Zdalne\menedzer_tray.py

Znajdz linie:
  NGROK_TOKEN = ""

Wklej token:
  NGROK_TOKEN = "2abc123_twoj_token_tutaj"

Zapisz plik.


KROK 5: Staly adres (wazne!)
------------------------------
Na darmowym planie ngrok daje staly adres dopiero
po zalogowaniu w dashboardzie i zarezerwowaniu go:

1. Zaloguj sie na dashboard.ngrok.com
2. Kliknij: Cloud Edge -> Domains
3. Kliknij: + New Domain
4. Dostaniesz adres np. "abc-xyz-123.ngrok-free.app" - jest Twoj na zawsze

Jesli nie zarezerw ujesz - adres bedzie sie zmieniac przy kazdym restarcie
(tak samo jak Cloudflare Quick Tunnel)


GOTOWE
-------
Po uruchomieniu Start.bat adres pojawi sie automatycznie na Telegramie.
Push notifications beda dzialac bo HTTPS jest prawdziwy.

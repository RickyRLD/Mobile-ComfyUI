JAK USTAWIC STALY ADRES CLOUDFLARE
====================================

Czas: ~5 minut. Darmowe. Raz na zawsze.

KROK 1: Zaloz darmowe konto Cloudflare
---------------------------------------
Wejdz na: https://dash.cloudflare.com/sign-up
Email + haslo. Nie potrzebujesz domeny.


KROK 2: Stworz tunel
----------------------
1. Zaloguj sie na: https://one.dash.cloudflare.com/
2. Po lewej: Networks -> Tunnels
3. Kliknij: "Create a tunnel"
4. Wybierz: "Cloudflared"
5. Nazwa tunelu: np. "comfyui" (dowolna)
6. Kliknij Save tunnel


KROK 3: Skopiuj token
-----------------------
Po zapisaniu tunelu pojawi sie ekran z tokenem.
Bedzie wygladac mniej wiecej tak:

  cloudflared.exe service install eyJhIjoiNzQ...dlugi ciag znakow...

Skopiuj TYLKO dlugi ciag znakow po "service install "
(zaczyna sie od eyJ...)


KROK 4: Skonfiguruj adres
---------------------------
1. Na tym samym ekranie kliknij "Next"
2. Public hostname -> Add a public hostname
3. Subdomain: wpisz co chcesz np. "ricky"
4. Domain: wybierz "cfargotunnel.com" (darmowe!)
   LUB jesli masz wlasna domene - wybierz ja
5. Service Type: HTTP
6. URL: localhost:8000
7. Kliknij Save


KROK 5: Wklej token do config.py
----------------------------------
Otworz plik: C:\AI\Zdalne\config.py

Znajdz linie:
  CLOUDFLARE_TUNNEL_TOKEN = ""

Wklej token:
  CLOUDFLARE_TUNNEL_TOKEN = "eyJhIjoiNzQ...twoj_token..."

Zapisz plik.


GOTOWE
-------
Od teraz po uruchomieniu serwera adres bedzie zawsze taki sam.
Nie musisz nic wiecej robic - token dziala na stale.

Twoj adres bedzie: https://ricky.cfargotunnel.com
(lub twoja subdomena ktora wybrales)

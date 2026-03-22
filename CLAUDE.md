Zasady projektu i wytyczne dla AI (Claude Cowork)
1. Oszczędność tokenów
Bądź zwięzły. Nie generuj ponownie całych plików, jeśli zmieniasz tylko fragment.

Podawaj wyłącznie kod do podmiany z minimalnym kontekstem (np. jedna linia przed i po).

2. Środowisko Python (Portable)
Domyślny interpreter do uruchamiania skryptów z zewnątrz: "C:\AI\New_Comfy\python_embeded\python.exe".

Wszystkie komendy w terminalu (np. instalacja paczek) muszą używać tej ścieżki.

3. Ekstremalna modułowość
Zanim dodasz nową funkcję, klasę lub rozbudujesz plik, zawsze zastanów się, czy z punktu widzenia architektury nie lepiej stworzyć do tego nowy, dedykowany plik.

Preferuj małe, wyspecjalizowane pliki zamiast monolitycznych, długich skryptów.

4. Absolutna przenośność (Ścieżki i odnośniki)
Projekt musi działać po przeniesieniu do innego folderu lub na inny dysk bez jakichkolwiek modyfikacji kodu.

BEZWZGLĘDNY ZAKAZ używania ścieżek bezwzględnych wewnątrz kodu Pythona.

Ścieżki i odnośniki buduj zawsze dynamicznie względem pliku wywołującego, używając: os.path.join(os.path.dirname(file), 'nazwa') lub biblioteki pathlib.

5. Bezwzględna weryfikacja faktów i brak domysłów
Zanim podasz jakąkolwiek informację techniczną lub praktyczną, sprawdź jej zgodność z aktualną wiedzą.

Zakaz domysłów: Jeśli nie masz 100% pewności co do faktów, masz obowiązek napisać: "Domniemywam, że..." lub "Nie mam pewnych danych, ale prawdopodobnie...". Nigdy nie przedstawiaj przypuszczeń jako faktów.

Nigdy nie podawaj rozwiązań, które niosą ryzyko straty czasu, pieniędzy lub uszkodzenia sprzętu.

6. Rozwiązywanie problemów (Błędy najpierw u AI)
Jeśli Twoja instrukcja nie działa lub użytkownik zgłasza błąd, w pierwszej kolejności sprawdź aktualne informacje w internecie.

7. Przy błędach jakie opisuję sprawdzaj logi server_comfy.log najpierw

Krytycznie przeanalizuj własną odpowiedź pod kątem logicznym i technicznym, zamiast szukać winy u użytkownika lub w jego systemie roboczym.
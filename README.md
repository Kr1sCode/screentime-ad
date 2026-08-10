# screentime-ad

Elastyczny dzienny budżet czasu komputera dla **jednego konta Active
Directory**, egzekwowany na dowolnej maszynie domenowej, na której to konto
się zaloguje (Windows i Linux). Inne konta na tych samych maszynach nie są
w ogóle ruszane.

- Jedna wspólna pula minut dziennie (nie okna godzinowe) — do wykorzystania
  kiedy chce, na dowolnym z komputerów.
- 5 minut przed wyczerpaniem: duży baner na 10 sekund z ostrzeżeniem.
- Po wyczerpaniu: wymuszone wylogowanie.
- Panel web: podgląd zużycia na żywo, ustawianie limitu, "+15 min", "zablokuj
  teraz", historia.
- Agenty (Windows + Linux) same aktualizują się z tego repo — nie ma numerów
  wersji do pilnowania, liczy się najnowszy commit dotykający ich katalogu.

## Architektura

```
[Windows PC] ─┐
[Linux (AD)] ─┼─ heartbeat (HTTPS + token) ──> serwer (Flask + SQLite szyfrowane) + panel
```

Zobacz `agent_linux/agent.py` i `agent_windows/agent.py` — jeden plik,
minimum zależności (Linux: stdlib + `zenity`/`loginctl`; Windows: stdlib +
`ctypes` na wtsapi32, bez pywin32).

## Serwer (panel)

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Kr1sCode/screentime-ad/main/install.sh)"
```

Domyślne dane logowania: `admin` / `admin` — zmień w panelu (Zmiana hasła).
Baza SQLite jest szyfrowana domyślnie (SQLCipher), klucz generowany
automatycznie przy pierwszym uruchomieniu (`/var/lib/screentime-ad/db.key`,
poza gitem).

W panelu (Konfiguracja) ustaw `sAMAccountName` śledzonego konta, dzienny
limit w minutach, i skopiuj token agenta.

## Agent — Linux (AD-joined, np. sssd)

```
sudo SCREENTIME_SERVER=http://<ip-serwera> SCREENTIME_TOKEN=<token> \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Kr1sCode/screentime-ad/main/agent_linux/install.sh)"
```

## Agent — Windows

PowerShell jako Administrator:

```
irm https://raw.githubusercontent.com/Kr1sCode/screentime-ad/main/agent_windows/install.ps1 | iex
```

Dla całej floty domenowej: ten sam skrypt jako **GPO Computer Startup
Script** (SYSVOL) — jest idempotentny, więc na maszynach gdzie usługa już
działa nic nie robi; nowe maszyny dostają agenta przy najbliższym boocie.

## Twarda blokada konta w AD (opcjonalnie)

Sam agent wylogowuje z otwartej sesji, ale nic nie stoi na przeszkodzie żeby
zalogować się ponownie — agent po prostu wyloguje znowu przy najbliższym
cyklu. Żeby zablokować logowanie NIGDZIE w domenie aż do resetu puli, włącz
w panelu wyłączanie konta w AD przez LDAP — patrz
[`docs/ad-lock-setup.md`](docs/ad-lock-setup.md) (wymaga dedykowanego konta
serwisowego z uprawnieniem tylko do `userAccountControl` na koncie syna).

## Ograniczenia (świadomie, poziom homelab)

- Brak twardej ochrony przed lokalnym adminem zatrzymującym usługę.
- Brak blokowania per-aplikacja/strona — czysty budżet czasu.
- Jeden globalny dzienny limit (bez harmonogramu per dzień tygodnia).

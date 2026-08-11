# screentime-ad

Elastyczny dzienny budżet czasu komputera dla **jednego konta Active
Directory**, egzekwowany na dowolnej maszynie domenowej, na której to konto
się zaloguje (Windows i Linux). Inne konta na tych samych maszynach nie są
w ogóle ruszane.

- Wspólna pula minut dziennie, osobny limit na każdy dzień tygodnia — do
  wykorzystania kiedy chce, na dowolnym z komputerów.
- Godziny blokady (twardy zakaz niezależny od puli, np. noc).
- 5 minut przed wyczerpaniem (albo przed startem godzin blokady): duży baner
  na 10 sekund z ostrzeżeniem.
- Po wyczerpaniu: wymuszone wylogowanie z otwartej sesji + opcjonalnie
  twarda blokada konta w AD (patrz niżej), żeby nie dało się zalogować
  ponownie.
- Panel web: podgląd zużycia na żywo, szybkie korekty (+15/-10/-20 min),
  ustawienie dokładnej wartości pozostałego czasu, ręczna blokada/odblokowanie,
  historia.
- Agent Linux aktualizuje się sam z tego repo (najnowszy commit, bez
  numerów wersji). Agent Windows to podpisany checksumem installer z
  GitHub Releases, z własnym auto-update (patrz niżej).

## Architektura

```
[Windows PC] ─┐
[Linux (AD)] ─┼─ heartbeat (HTTPS + token) ──> serwer (Flask + SQLite szyfrowane) + panel
```

Zobacz `agent_linux/agent.py` (jeden plik, stdlib + `zenity`/`loginctl`) i
`agent_windows/agent_service.py` + `agent_windows/tray.py` (stdlib + `ctypes`
na wtsapi32/Shell_NotifyIcon, bez pywin32) — usługa (SYSTEM, widzi wszystkie
sesje) plus osobna ikona w trayu per-user (status/pozostały czas na hover),
połączone przez wspólny `status.json`.

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

Pobierz i uruchom najnowszy `screentime-ad-agent-setup.exe` ze
[strony Releases](https://github.com/Kr1sCode/screentime-ad/releases/latest)
(GUI installer — pyta o adres serwera i token). Instaluje usługę (NSSM,
autostart) + ikonę w trayu (Scheduled Task, uruchamia się każdemu
zalogowanemu userowi). Ma normalny wpis w **Panel sterowania → Programy i
funkcje** (autor, wersja, deinstalacja).

Auto-update: usługa sama sprawdza co godzinę najnowszy release na GitHubie,
weryfikuje sha256 installera względem `SHA256SUMS.txt` z tego samego
release'a, i po dopasowaniu odpala go cicho (`/VERYSILENT`) — Inno Setup
sam zatrzymuje usługę, podmienia pliki, instaluje ponownie, startuje.

Dla całej floty domenowej: ten sam installer jako **GPO Computer Startup
Script** wywołujący go z `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` —
idempotentny (Inno pomija reinstalację jeśli wersja się zgadza).

Wydanie nowej wersji (dla utrzymującego): zbij `VERSION`, `git tag v0.1.1 &&
git push --tags` — CI (`.github/workflows/build-windows.yml`) sam zbuduje
installer i dołączy go razem z `SHA256SUMS.txt` do GitHub Release.

## Twarda blokada konta w AD (opcjonalnie)

Sam agent wylogowuje z otwartej sesji, ale nic nie stoi na przeszkodzie żeby
zalogować się ponownie — agent po prostu wyloguje znowu przy najbliższym
cyklu. Żeby zablokować logowanie NIGDZIE w domenie aż do resetu puli, włącz
w panelu wyłączanie konta w AD przez LDAP — patrz
[`docs/ad-lock-setup.md`](docs/ad-lock-setup.md) (wymaga dedykowanego konta
serwisowego z uprawnieniem tylko do `userAccountControl` na koncie dziecka).

## Ograniczenia (świadomie, poziom homelab)

- Brak twardej ochrony przed lokalnym adminem zatrzymującym usługę (poza
  opcjonalną blokadą konta w AD, która działa niezależnie od tego czy
  agent na danej maszynie w ogóle żyje).
- Brak blokowania per-aplikacja/strona — czysty budżet czasu.
- `tray.py` (ikona w trayu) to jedyny fragment kodu, którego nie dało się
  przetestować na żywym Windows z tej sesji — czysty ctypes Win32 GUI, jeśli
  coś nie działa, sprawdź `%ProgramData%\screentime-ad\tray.log`.

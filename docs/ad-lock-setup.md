# Twarda blokada konta w AD — konfiguracja na dc-example

Uruchom jako Domain Admin (RSAT `ActiveDirectory` module) **na/przeciw dc-example**.
Tworzy jedno konto serwisowe z uprawnieniem **wyłącznie** do zmiany atrybutu
`userAccountControl` na koncie syna — nic więcej (nie hasło, nie grupy, nie
inne atrybuty, nie inne konta).

```powershell
# 0) Sprawdź prawdziwą nazwę NetBIOS domeny (podstaw niżej zamiast "CONTOSO"
#    jeśli inna) i sAMAccountName konta syna:
Get-ADDomain | Select-Object NetBIOSName, DistinguishedName

# 1) Konto serwisowe
$pass = Read-Host -AsSecureString "Hasło dla svc-screentime"
New-ADUser -Name "svc-screentime" -SamAccountName "svc-screentime" `
  -AccountPassword $pass -Enabled $true -PasswordNeverExpires $true

# 2) Delegacja: TYLKO prawo do zapisu userAccountControl na koncie syna
#    (podstaw prawdziwy sAMAccountName syna)
$sonDN  = (Get-ADUser -Identity "SAMACCOUNTNAME_SYNA").DistinguishedName
dsacls "$sonDN" /G "CONTOSO\svc-screentime:WP;userAccountControl"

# 3) Weryfikacja delegacji
dsacls "$sonDN" | Select-String "svc-screentime"
```

## Dane do panelu (Twarda blokada konta w AD)

| Pole | Wartość |
|---|---|
| host LDAPS | `192.0.2.10` (dc-example) |
| base DN | Distinguished Name domeny z kroku 0, np. `DC=contoso,DC=...` |
| bind DN | `CN=svc-screentime,CN=Users,<base DN>` |
| hasło bind | hasło ustawione w kroku 1 |

Po zapisaniu kliknij **testuj połączenie** w panelu — spróbuje się zalogować i
wykonać nieszkodliwy zapis (odblokowanie, no-op jeśli konto już odblokowane).
Błąd `insufficientAccessRights` = delegacja z kroku 2 nie poszła / zła DN.

Cert LDAPS na dc-example bywa wygasły — agent (`ad_lock.py`) świadomie nie
weryfikuje certyfikatu (`ssl.CERT_NONE`), bo to ruch wewnątrz zaufanej sieci
domenowej, nie coś wystawionego na zewnątrz.

Dopiero po udanym teście zaznacz **włącz twardą blokadę AD** — inaczej
serwer nic nie robi (flaga wyłączona domyślnie, celowo, żeby nie zablokować
nikomu konta przez niedokończoną konfigurację).

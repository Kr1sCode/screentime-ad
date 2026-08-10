"""Twarda blokada logowania: wyłącza/włącza konto w AD przez LDAP, żeby po
wyczerpaniu puli syn nie mógł się zalogować NIGDZIE w domenie (nie tylko
dostać wylogowanego z już otwartej sesji — to robią agenty, osobno)."""
import ssl

from ldap3 import MODIFY_REPLACE, Connection, Server, Tls

ACCOUNTDISABLE = 0x2


def _connect(cfg: dict) -> Connection:
    tls = Tls(validate=ssl.CERT_NONE)  # LDAPS na wewnętrznym DC, cert bywa wygasły/self-signed
    server = Server(cfg["ldap_host"], use_ssl=True, tls=tls)
    return Connection(server, user=cfg["bind_dn"], password=cfg["bind_password"], auto_bind=True)


def sync_account_disabled(cfg: dict, samaccountname: str, disabled: bool) -> bool:
    """Ustawia bit ACCOUNTDISABLE na koncie. Zwraca True jeśli coś zmieniono."""
    conn = _connect(cfg)
    try:
        conn.search(cfg["base_dn"], f"(sAMAccountName={samaccountname})", attributes=["userAccountControl"])
        if not conn.entries:
            raise RuntimeError(f"nie znaleziono konta AD {samaccountname!r} pod {cfg['base_dn']}")
        entry = conn.entries[0]
        uac = int(entry.userAccountControl.value)
        new_uac = (uac | ACCOUNTDISABLE) if disabled else (uac & ~ACCOUNTDISABLE)
        if new_uac == uac:
            return False
        conn.modify(entry.entry_dn, {"userAccountControl": [(MODIFY_REPLACE, [new_uac])]})
        return True
    finally:
        conn.unbind()
